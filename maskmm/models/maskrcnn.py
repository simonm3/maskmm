import datetime
import os
import re

import numpy as np
import torch
import torch.nn as nn
import torch.utils.data

from maskmm.utils import image_utils
from maskmm.utils.batch import batch_slice

from maskmm.datagen.head_targets import build_head_targets
from maskmm.filters.proposals import proposals
from maskmm.filters.detections import get_detections
from maskmm.filters.roialign import roialign

from .rpn import RPN
from .resnet import ResNet
from .resnetFPN import FPN
from .head import Classifier, Mask

from maskmm.baseline import save

import logging
log = logging.getLogger()


class MaskRCNN(nn.Module):
    """Encapsulates the Mask RCNN model functionality.
    """

    def __init__(self, config, model_dir):
        """
        config: A Sub-class of the Config class
        model_dir: Directory to save training logs and trained weights
        """
        super().__init__()
        self.config = config
        self.model_dir = model_dir
        self.set_log_dir()

        # Image size must be dividable by 2 multiple times
        h, w = config.IMAGE_SHAPE[:2]
        if h / 2 ** 6 != int(h / 2 ** 6) or w / 2 ** 6 != int(w / 2 ** 6):
            raise Exception("Image size must be dividable by 2 at least 6 times "
                            "to avoid fractions when downscaling and upscaling."
                            "For example, use 256, 320, 384, 448, 512, ... etc. ")

        # Build the shared convolutional layers.
        # Bottom-up Layers
        # Returns a list of the last layers of each stage, 5 in total.
        # Don't create the head (stage 5), so we pick the 4th item in the list.
        resnet = ResNet("resnet101", stage5=True)
        C1, C2, C3, C4, C5 = resnet.stages()

        # backbone
        self.fpn = FPN(C1, C2, C3, C4, C5, out_channels=256)

        # RPN
        self.rpn = RPN(len(config.RPN_ANCHOR_RATIOS), config.RPN_ANCHOR_STRIDE, 256)

        # head Classifier
        self.classifier = Classifier(256, config.POOL_SIZE, config.IMAGE_SHAPE, config.NUM_CLASSES)

        # head Mask
        self.mask = Mask(256, config.MASK_POOL_SIZE, config.IMAGE_SHAPE, config.NUM_CLASSES)

    def forward(self, *inputs):

        targets = len(inputs)>2

        if targets:
            # training/validation mode. inputs from dataloader/dataset
            # tgt_rpn_match and tgt_rpn_bbox not used but passed through because.....
            # fastai loss is calculated in callback to store results but this has single param output of this function.
            # fastai loss_func is not able to store intermediate results
            images, image_metas,\
            tgt_rpn_match, tgt_rpn_bbox, \
            gt_class_ids, gt_boxes, gt_masks = inputs
        else:
            images, image_metas = inputs

        config = self.config

        # Feature extraction
        feature_maps = self.fpn(images)

        # Loop through pyramid layers
        layer_outputs = []  # list of lists
        for p in feature_maps:
            layer_outputs.append(self.rpn(p))

        # Concatenate layer outputs
        # Convert from list of lists of level outputs to list of lists
        # of outputs across levels.
        # e.g. [[a1, b1, c1], [a2, b2, c2]] => [[a1, a2], [b1, b2], [c1, c2]]
        outputs = list(zip(*layer_outputs))
        outputs = [torch.cat(list(o), dim=1) for o in outputs]
        rpn_class_logits, rpn_class, rpn_bbox = outputs

        # Generate proposals
        # Proposals are [batch, N, (y1, x1, y2, x2)] in normalized coordinates
        # and zero padded.
        proposal_count = config.POST_NMS_ROIS_TRAINING if self.training \
            else config.POST_NMS_ROIS_INFERENCE
        rpn_rois = proposals(rpn_class, rpn_bbox, proposal_count=proposal_count, config=config)

        if not config.HEAD:
            return dict(out=[tgt_rpn_match, tgt_rpn_bbox, \
                             rpn_class_logits, rpn_bbox, 0,0,0,0,0,0])

        mrcnn_class_logits = torch.empty(0)
        mrcnn_probs = torch.empty(0).int()
        mrcnn_deltas = torch.empty(0)
        mrcnn_mask = torch.empty(0)

        if targets:
            with torch.no_grad():
                # Subsample proposals, generate target outputs for training and filter rois
                # inputs are zero padded.
                # output rois are stacked; rest are concatenated

                rois, target_class_ids, target_deltas, target_mask = \
                    build_head_targets(rpn_rois, gt_class_ids, gt_boxes, gt_masks, config)
        else:
            rois = rpn_rois

        # crop feature maps for each roi. NOTE drop last featuremap for head
        x = roialign(rois, *feature_maps[:-1], config.POOL_SIZE, config.IMAGE_SHAPE)
        mrcnn_class_logits, mrcnn_probs, mrcnn_deltas = batch_slice()(self.classifier)(x)

        if targets:
            # mask roialign. NOTE drop last featuremap for head
            x = roialign(rois, *feature_maps[:-1], config.MASK_POOL_SIZE, config.IMAGE_SHAPE)
            mrcnn_mask = batch_slice()(self.mask)(x)

            return dict(out=[tgt_rpn_match, tgt_rpn_bbox, \
                             rpn_class_logits, rpn_bbox, \
                             target_class_ids, target_deltas, target_mask, \
                             mrcnn_class_logits, mrcnn_deltas, mrcnn_mask])
        else:
            # detections filter speeds inference and improves accuracy (see maskrcnn paper)
            #### putting this after mask head is much worse!!!
            # note boxes are image domain for output. rois are scaled.
            boxes, class_ids, scores, rois = get_detections(rois, mrcnn_probs, mrcnn_deltas, image_metas, config)

            # Create masks for the selected boxes
            x = roialign(rois, *feature_maps[:-1], config.MASK_POOL_SIZE, config.IMAGE_SHAPE)
            masks = batch_slice()(self.mask)(x)

            return boxes, class_ids, scores, masks

    def predict(self, images):
        # predict list of images without targets, bypassing dataset
        if not isinstance(images, list):
            images = [images]

        # prepare inputs without using dataset
        molded_images = []
        image_shapes = []
        image_metas = []
        for image in images:
            image_shapes.append(torch.tensor(image.shape))
            image, window, scale, padding = image_utils.resize_image(image, self.config)
            image = image_utils.mold_image(image, self.config)
            molded_images.append(image)
            image_meta = image_utils.mold_meta(dict(window=window))
            image_metas.append(image_meta)
        molded_images = torch.stack(molded_images)
        image_metas = torch.stack(image_metas)
        image_shapes = torch.stack(image_shapes)

         # predict
        with torch.no_grad():
            boxes, class_ids, scores, masks = self(molded_images, image_metas)

        # prepare outputs
        results = []
        for i in range(len(images)):
            detections1 = [var[i] for var in [boxes, class_ids, scores, masks, image_shapes, image_metas]]
            results.append(image_utils.unmold_detections(*detections1))
        return results

    def initialize_weights(self):
        """Initialize model weights.
        """
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.xavier_uniform_(m.weight)
                if m.bias is not None:
                    m.bias.data.zero_()
            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()
            elif isinstance(m, nn.Linear):
                m.weight.data.normal_(0, 0.01)
                m.bias.data.zero_()

    def set_trainable(self, layer_regex):
        """Sets model layers as trainable if their names match
        the given regular expression.
        """

        for param in self.named_parameters():
            layer_name = param[0]
            trainable = bool(re.fullmatch(layer_regex, layer_name))
            if not trainable:
                param[1].requires_grad = False

    def set_log_dir(self, model_path=None):
        """Sets the model log directory and epoch counter.

        model_path: If None, or a format different from what this code uses
            then set a new log directory and start epochs from 0. Otherwise,
            extract the log directory and the epoch counter from the file
            name.
        """

        # Set date and epoch counter as if starting a new model
        self.epoch = 0
        now = datetime.datetime.now()

        # If we have a model path with date and epochs use them
        if model_path:
            # Continue from we left of. Get epoch and date from the file name
            # A sample model path might look like:
            # /path/to/logs/coco20171029T2315/mask_rcnn_coco_0001.h5
            regex = r".*/\w+(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})/mask\_rcnn\_\w+(\d{4})\.pth"
            m = re.match(regex, model_path)
            if m:
                now = datetime.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                                        int(m.group(4)), int(m.group(5)))
                self.epoch = int(m.group(6))

        # Directory for training logs
        self.log_dir = os.path.join(self.model_dir,
                                    f"{self.config.NAME.lower()}{datetime.datetime.now().strftime('%Y%m%d_%H%M')}")
        os.makedirs(self.log_dir, exist_ok=True)

        # Path to save after each epoch. Include placeholders that get filled by Keras.
        self.checkpoint_path = os.path.join(self.log_dir,
                                            "mask_rcnn_{}_*epoch*.pth".format(
                                                self.config.NAME.lower()))
        self.checkpoint_path = self.checkpoint_path.replace(
            "*epoch*", "{:04d}")

    def find_last(self):
        """Finds the last checkpoint file of the last trained model in the
        model directory.
        Returns:
            log_dir: The directory where events and weights are saved
            checkpoint_path: the path to the last checkpoint file
        """
        # Get directory names. Each directory corresponds to a model
        dir_names = next(os.walk(self.model_dir))[1]
        key = self.config.NAME.lower()
        dir_names = filter(lambda f: f.startswith(key), dir_names)
        dir_names = sorted(dir_names)
        if not dir_names:
            return None, None
        # Pick last directory
        dir_name = os.path.join(self.model_dir, dir_names[-1])
        # Find the last checkpoint
        checkpoints = next(os.walk(dir_name))[2]
        checkpoints = filter(lambda f: f.startswith("mask_rcnn"), checkpoints)
        checkpoints = sorted(checkpoints)
        if not checkpoints:
            return dir_name, None
        checkpoint = os.path.join(dir_name, checkpoints[-1])
        return dir_name, checkpoint

    def load_weights(self, filepath):
        """Modified version of the correspoding Keras function with
        the addition of multi-GPU support and the ability to exclude
        some layers from loading.
        exlude: list of layer names to excluce
        """
        if os.path.exists(filepath):
            state_dict = torch.load(filepath)
            self.load_state_dict(state_dict, strict=False)
        else:
            print("Weight file not found ...")

        # Update the log directory
        self.set_log_dir(filepath)
        if not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir)
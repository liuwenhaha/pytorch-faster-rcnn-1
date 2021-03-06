# coding=utf-8
# --------------------------------------------------------
# Tensorflow Faster R-CNN
# Licensed under The MIT License [see LICENSE for details]
# Written by Xinlei Chen
# --------------------------------------------------------
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math
import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable

import utils.timer

from layer_utils.snippets import generate_anchors_pre
from layer_utils.proposal_layer import proposal_layer
from layer_utils.proposal_top_layer import proposal_top_layer
from layer_utils.anchor_target_layer import anchor_target_layer
from layer_utils.proposal_target_layer import proposal_target_layer
from utils.visualization import draw_bounding_boxes

from layer_utils.roi_pooling.roi_pool import RoIPoolFunction

from model.config import cfg

import tensorboardX as tb

from scipy.misc import imresize

class Network(nn.Module):
  def __init__(self):
    nn.Module.__init__(self)
    self._predictions = {}
    self._losses = {}
    self._anchor_targets = {}
    self._proposal_targets = {}
    self._layers = {}
    self._gt_image = None
    self._act_summaries = {}
    self._score_summaries = {}
    self._event_summaries = {}
    self._image_gt_summaries = {}
    self._variables_to_fix = {}

  def _add_gt_image(self):
    # add back mean
    image = self._image_gt_summaries['image'] + cfg.PIXEL_MEANS
    image = imresize(image[0], self._im_info[:2] / self._im_info[2])
    # BGR to RGB (opencv uses BGR)
    self._gt_image = image[np.newaxis, :,:,::-1].copy(order='C')

  def _add_gt_image_summary(self):
    # use a customized visualization function to visualize the boxes
    self._add_gt_image()
    image = draw_bounding_boxes(\
                      self._gt_image, self._image_gt_summaries['gt_boxes'], self._image_gt_summaries['im_info'])

    return tb.summary.image('GROUND_TRUTH', image[0].astype('float32')/255.0)

  def _add_act_summary(self, key, tensor):
    return tb.summary.histogram('ACT/' + key + '/activations', tensor.data.cpu().numpy(), bins='auto'),
    tb.summary.scalar('ACT/' + key + '/zero_fraction',
                      (tensor.data == 0).float().sum() / tensor.numel())

  def _add_score_summary(self, key, tensor):
    return tb.summary.histogram('SCORE/' + key + '/scores', tensor.data.cpu().numpy(), bins='auto')

  def _add_train_summary(self, key, var):
    return tb.summary.histogram('TRAIN/' + key, var.data.cpu().numpy(), bins='auto')

  def _proposal_top_layer(self, rpn_cls_prob, rpn_bbox_pred):
    rois, rpn_scores = proposal_top_layer(\
                                    rpn_cls_prob, rpn_bbox_pred, self._im_info,
                                     self._feat_stride, self._anchors, self._num_anchors)
    return rois, rpn_scores

  def _proposal_layer(self, rpn_cls_prob, rpn_bbox_pred):
    rois, rpn_scores = proposal_layer(\
                                    rpn_cls_prob, rpn_bbox_pred, self._im_info, self._mode,
                                     self._feat_stride, self._anchors, self._num_anchors)

    return rois, rpn_scores

  def _roi_pool_layer(self, bottom, rois):
    return RoIPoolFunction(cfg.POOLING_SIZE, cfg.POOLING_SIZE, 1. / 16.)(bottom, rois)
 # mode nearest, bilinear
  def _crop_pool_layer(self, bottom, rois, scaling_ratio=16.0, mode='bilinear', max_pool=True, use_for_parsing=False):
    # implement it using stn
    # box to affine
    # input (x1,y1,x2,y2)
    """
    [  x2-x1             x1 + x2 - W + 1  ]
    [  -----      0      ---------------  ]
    [  W - 1                  W - 1       ]
    [                                     ]
    [           y2-y1    y1 + y2 - H + 1  ]
    [    0      -----    ---------------  ]
    [           H - 1         H - 1      ]
    """
    rois = rois.detach()

    x1 = rois[:, 1::4] / scaling_ratio  # 16.0
    y1 = rois[:, 2::4] / scaling_ratio  # 16.0
    x2 = rois[:, 3::4] / scaling_ratio  # 16.0
    y2 = rois[:, 4::4] / scaling_ratio  # 16.0

    height = bottom.size(2)
    width = bottom.size(3)
    # affine theta
    theta = Variable(rois.data.new(rois.size(0), 2, 3).zero_())
    theta[:, 0, 0] = (x2 - x1) / (width - 1)
    theta[:, 0, 2] = (x1 + x2 - width + 1) / (width - 1)
    theta[:, 1, 1] = (y2 - y1) / (height - 1)
    theta[:, 1, 2] = (y1 + y2 - height + 1) / (height - 1)
    if use_for_parsing:
      pre_pool_size = cfg.POOLING_SIZE * 4
      grid = F.affine_grid(theta, torch.Size((rois.size(0), 1, pre_pool_size, pre_pool_size)))
      crops = F.grid_sample(bottom.expand(rois.size(0), bottom.size(1), bottom.size(2), bottom.size(3)), grid,
                            mode=mode)
    else:
      if max_pool:
        pre_pool_size = cfg.POOLING_SIZE * 2
        grid = F.affine_grid(theta, torch.Size((rois.size(0), 1, pre_pool_size, pre_pool_size)))
        crops = F.grid_sample(bottom.expand(rois.size(0), bottom.size(1), bottom.size(2), bottom.size(3)), grid, mode=mode)
        crops = F.max_pool2d(crops, 2, 2)
      else:
        grid = F.affine_grid(theta, torch.Size((rois.size(0), 1, cfg.POOLING_SIZE, cfg.POOLING_SIZE)))
        crops = F.grid_sample(bottom.expand(rois.size(0), bottom.size(1), bottom.size(2), bottom.size(3)), grid)
    
    return crops

  def _anchor_target_layer(self, rpn_cls_score):
    rpn_labels, rpn_bbox_targets, rpn_bbox_inside_weights, rpn_bbox_outside_weights = \
      anchor_target_layer(
      rpn_cls_score.data, self._gt_boxes.data.cpu().numpy(), self._im_info, self._feat_stride, self._anchors.data.cpu().numpy(), self._num_anchors)

    rpn_labels = Variable(torch.from_numpy(rpn_labels).float().cuda()) #.set_shape([1, 1, None, None])
    rpn_bbox_targets = Variable(torch.from_numpy(rpn_bbox_targets).float().cuda())#.set_shape([1, None, None, self._num_anchors * 4])
    rpn_bbox_inside_weights = Variable(torch.from_numpy(rpn_bbox_inside_weights).float().cuda())#.set_shape([1, None, None, self._num_anchors * 4])
    rpn_bbox_outside_weights = Variable(torch.from_numpy(rpn_bbox_outside_weights).float().cuda())#.set_shape([1, None, None, self._num_anchors * 4])

    rpn_labels = rpn_labels.long()
    self._anchor_targets['rpn_labels'] = rpn_labels
    self._anchor_targets['rpn_bbox_targets'] = rpn_bbox_targets
    self._anchor_targets['rpn_bbox_inside_weights'] = rpn_bbox_inside_weights
    self._anchor_targets['rpn_bbox_outside_weights'] = rpn_bbox_outside_weights

    for k in self._anchor_targets.keys():
      self._score_summaries[k] = self._anchor_targets[k]

    return rpn_labels

  def _proposal_target_layer(self, rois, roi_scores):
    if cfg.SUB_CATEGORY:
      if cfg.DO_PARSING:
        rois, roi_scores, labels, sub_labels, bbox_targets, bbox_inside_weights, bbox_outside_weights, mask_unit = \
          proposal_target_layer(
            rois, roi_scores, self._gt_boxes, self._num_classes, self._parsing_labels)
      else:
        rois, roi_scores, labels, sub_labels, bbox_targets, bbox_inside_weights, bbox_outside_weights = \
          proposal_target_layer(
            rois, roi_scores, self._gt_boxes, self._num_classes)
    else:
      if cfg.DO_PARSING:
        rois, roi_scores, labels, bbox_targets, bbox_inside_weights, bbox_outside_weights, mask_unit = \
          proposal_target_layer(
            rois, roi_scores, self._gt_boxes, self._num_classes, self._parsing_labels)
      else:
        rois, roi_scores, labels, bbox_targets, bbox_inside_weights, bbox_outside_weights = \
          proposal_target_layer(
          rois, roi_scores, self._gt_boxes, self._num_classes)
    if cfg.SUB_CATEGORY:
      self._proposal_targets['sub_labels'] = sub_labels.long()
    if cfg.DO_PARSING:
      self._proposal_targets['mask_unit'] = mask_unit

    self._proposal_targets['rois'] = rois
    self._proposal_targets['labels'] = labels.long()
    self._proposal_targets['bbox_targets'] = bbox_targets
    self._proposal_targets['bbox_inside_weights'] = bbox_inside_weights
    self._proposal_targets['bbox_outside_weights'] = bbox_outside_weights

    for k in self._proposal_targets.keys():
      self._score_summaries[k] = self._proposal_targets[k]

    return rois, roi_scores

  def _anchor_component(self, height, width):
    # just to get the shape right
    #height = int(math.ceil(self._im_info.data[0, 0] / self._feat_stride[0]))
    #width = int(math.ceil(self._im_info.data[0, 1] / self._feat_stride[0]))
    anchors, anchor_length = generate_anchors_pre(\
                                          height, width,
                                           self._feat_stride, self._anchor_scales, self._anchor_ratios)
    self._anchors = Variable(torch.from_numpy(anchors).cuda())
    self._anchor_length = anchor_length

  def _smooth_l1_loss(self, bbox_pred, bbox_targets, bbox_inside_weights, bbox_outside_weights, sigma=1.0, dim=[1]):
    sigma_2 = sigma ** 2
    box_diff = bbox_pred - bbox_targets
    in_box_diff = bbox_inside_weights * box_diff
    abs_in_box_diff = torch.abs(in_box_diff)
    smoothL1_sign = (abs_in_box_diff < 1. / sigma_2).detach().float()
    in_loss_box = torch.pow(in_box_diff, 2) * (sigma_2 / 2.) * smoothL1_sign \
                  + (abs_in_box_diff - (0.5 / sigma_2)) * (1. - smoothL1_sign)
    out_loss_box = bbox_outside_weights * in_loss_box
    loss_box = out_loss_box
    for i in sorted(dim, reverse=True):
      loss_box = loss_box.sum(i)
    loss_box = loss_box.mean()
    return loss_box

  def _add_losses(self, sigma_rpn=3.0):
    # RPN, class loss
    rpn_cls_score = self._predictions['rpn_cls_score_reshape'].view(-1, 2)
    rpn_label = self._anchor_targets['rpn_labels'].view(-1)
    rpn_select = Variable((rpn_label.data != -1).nonzero().view(-1))
    rpn_cls_score = rpn_cls_score.index_select(0, rpn_select).contiguous().view(-1, 2)
    rpn_label = rpn_label.index_select(0, rpn_select).contiguous().view(-1)
    rpn_cross_entropy = F.cross_entropy(rpn_cls_score, rpn_label)

    # RPN, bbox loss
    rpn_bbox_pred = self._predictions['rpn_bbox_pred']
    rpn_bbox_targets = self._anchor_targets['rpn_bbox_targets']
    rpn_bbox_inside_weights = self._anchor_targets['rpn_bbox_inside_weights']
    rpn_bbox_outside_weights = self._anchor_targets['rpn_bbox_outside_weights']
    rpn_loss_box = self._smooth_l1_loss(rpn_bbox_pred, rpn_bbox_targets, rpn_bbox_inside_weights,
                                          rpn_bbox_outside_weights, sigma=sigma_rpn, dim=[1, 2, 3])

    # RCNN, class loss
    cls_score = self._predictions["cls_score"]
    label = self._proposal_targets["labels"].view(-1)
    cross_entropy = F.cross_entropy(cls_score.view(-1, self._num_classes), label)

    # RCNN, bbox loss
    bbox_pred = self._predictions['bbox_pred']
    bbox_targets = self._proposal_targets['bbox_targets']
    bbox_inside_weights = self._proposal_targets['bbox_inside_weights']
    bbox_outside_weights = self._proposal_targets['bbox_outside_weights']
    loss_box = self._smooth_l1_loss(bbox_pred, bbox_targets, bbox_inside_weights, bbox_outside_weights)

    self._losses['cross_entropy'] = cross_entropy
    self._losses['loss_box'] = loss_box
    self._losses['rpn_cross_entropy'] = rpn_cross_entropy
    self._losses['rpn_loss_box'] = rpn_loss_box

    loss = cross_entropy + loss_box + rpn_cross_entropy + rpn_loss_box
    # parsing loss
    if cfg.DO_PARSING:
      mask_score_map = self._predictions["mask_score_map"]
      gt_channel = self._proposal_targets['mask_unit']['mask_cls_labels'].data.long()
      gt = gt_channel[0]
      mask_score_map_gt_channel = mask_score_map[0:1, gt:gt+1, :, :]
      for i in range(1,len(gt_channel)):
        gt = gt_channel[i]
        mask_score_map_gt_channel = torch.cat((mask_score_map_gt_channel,mask_score_map[i:i+1, gt:gt+1, :, :]), 0)

      # mask_score_map_gt_channel = F.sigmoid(mask_score_map_gt_channel)

      #print(self._proposal_targets['mask_unit']['mask_parsing_labels'].size())
      parsing_loss = F.binary_cross_entropy(mask_score_map_gt_channel, self._proposal_targets['mask_unit']["mask_parsing_labels"])
      self._losses['parsing_loss'] = parsing_loss
      loss = loss + parsing_loss
    # RCNN sub_category loss
    if cfg.SUB_CATEGORY:
      cls_sub_score = self._predictions["cls_sub_score"]
      sub_label = self._proposal_targets["sub_labels"].view(-1)
      sub_cross_entropy = F.cross_entropy(cls_sub_score.view(-1, (self._num_classes - 1) * 3 + 1), sub_label)
      self._losses['sub_cross_entropy'] = sub_cross_entropy
      loss = loss + cfg.LOSS_SUB_CATEGORY_W * sub_cross_entropy
    self._losses['total_loss'] = loss

    for k in self._losses.keys():
      self._event_summaries[k] = self._losses[k]

    return loss

  def _region_proposal(self, net_conv):
    rpn = F.relu(self.rpn_net(net_conv))
    self._act_summaries['rpn'] = rpn

    rpn_cls_score = self.rpn_cls_score_net(rpn) # batch * (num_anchors * 2) * h * w

    # change it so that the score has 2 as its channel size
    rpn_cls_score_reshape = rpn_cls_score.view(1, 2, -1, rpn_cls_score.size()[-1]) # batch * 2 * (num_anchors*h) * w
    rpn_cls_prob_reshape = F.softmax(rpn_cls_score_reshape)
    
    # Move channel to the last dimenstion, to fit the input of python functions
    rpn_cls_prob = rpn_cls_prob_reshape.view_as(rpn_cls_score).permute(0, 2, 3, 1) # batch * h * w * (num_anchors * 2)
    rpn_cls_score = rpn_cls_score.permute(0, 2, 3, 1) # batch * h * w * (num_anchors * 2)
    rpn_cls_score_reshape = rpn_cls_score_reshape.permute(0, 2, 3, 1).contiguous()  # batch * (num_anchors*h) * w * 2
    rpn_cls_pred = torch.max(rpn_cls_score_reshape.view(-1, 2), 1)[1]

    rpn_bbox_pred = self.rpn_bbox_pred_net(rpn)
    rpn_bbox_pred = rpn_bbox_pred.permute(0, 2, 3, 1).contiguous()  # batch * h * w * (num_anchors*4)

    if self._mode == 'TRAIN':
      # rois (300,5) 0 batch 1-4 坐标
      rois, roi_scores = self._proposal_layer(rpn_cls_prob, rpn_bbox_pred) # rois, roi_scores are varible
      #print('proposal: roi: ', rois.size(), 'roi_scores: ', roi_scores.size())
      rpn_labels = self._anchor_target_layer(rpn_cls_score)
      rois, _ = self._proposal_target_layer(rois, roi_scores)
      #print('proposal_target: roi', rois.size())
    else:
      if cfg.TEST.MODE == 'nms':
        rois, _ = self._proposal_layer(rpn_cls_prob, rpn_bbox_pred)
        mask_unit= {}
        mask_unit['mask_rois'] = rois
        self._proposal_targets['mask_unit'] = mask_unit
      elif cfg.TEST.MODE == 'top':
        rois, _ = self._proposal_top_layer(rpn_cls_prob, rpn_bbox_pred)
      else:
        raise NotImplementedError

    self._predictions["rpn_cls_score"] = rpn_cls_score
    self._predictions["rpn_cls_score_reshape"] = rpn_cls_score_reshape
    self._predictions["rpn_cls_prob"] = rpn_cls_prob
    self._predictions["rpn_cls_pred"] = rpn_cls_pred
    self._predictions["rpn_bbox_pred"] = rpn_bbox_pred
    self._predictions["rois"] = rois

    return rois

  def _region_classification(self, fc7):
    cls_score = self.cls_score_net(fc7)
    if cfg.SUB_CATEGORY:
      cls_sub_score = self.cls_sub_score_net(fc7)
    cls_pred = torch.max(cls_score, 1)[1]
    cls_prob = F.softmax(cls_score)
    bbox_pred = self.bbox_pred_net(fc7)

    self._predictions["cls_score"] = cls_score
    self._predictions["cls_pred"] = cls_pred
    self._predictions["cls_prob"] = cls_prob
    self._predictions["bbox_pred"] = bbox_pred
    if cfg.SUB_CATEGORY:
      self._predictions["cls_sub_score"] = cls_sub_score
    return cls_prob, bbox_pred

  def _image_to_head(self):
    raise NotImplementedError

  def _head_to_tail(self, pool5):
    raise NotImplementedError

  def _parsing_net(self, input):
    # output = self.mask_deconv1(input)
    # output = self.mask_bn0(output)
    # output = F.relu(output)

    output = self.mask_conv1(input)
    output = self.mask_bn1(output)
    output = F.relu(output)

    output = self.mask_conv2(output)
    output = self.mask_bn2(output)
    output = F.relu(output)

    output = self.mask_conv3(output)
    output = self.mask_bn3(output)
    output = F.relu(output)

    output = self.mask_conv4(output)
    output = self.mask_bn4(output)
    output = F.relu(output)

    output = self.mask_deconv2(output)
    output = F.relu(output)
    output = self.mask_conv5(output)

    output = F.sigmoid(output)
    # output = torch.round(output)

    return output
  def create_architecture(self, num_classes, tag=None,
                          anchor_scales=(8, 16, 32), anchor_ratios=(0.5, 1, 2)):
    self._tag = tag

    self._num_classes = num_classes
    self._anchor_scales = anchor_scales
    self._num_scales = len(anchor_scales)

    self._anchor_ratios = anchor_ratios
    self._num_ratios = len(anchor_ratios)

    self._num_anchors = self._num_scales * self._num_ratios

    assert tag != None

    # Initialize layers
    self._init_modules()

  def _init_modules(self):
    self._init_head_tail()
    if cfg.LIGHT_RCNN:
      cmid = 128
      cout = cfg.FC6_IN_CHANNEL
      self.lightrcnn_conv1 = nn.Conv2d(512, cmid, [15, 1], padding=(7, 0))
      self.lightrcnn_conv2 = nn.Conv2d(cmid, cout, [1, 15], padding=(0, 7))
      self.lightrcnn_conv3 = nn.Conv2d(512, cmid, [1, 15], padding=(0, 7))
      self.lightrcnn_conv4 = nn.Conv2d(cmid, cout, [15, 1], padding=(7, 0))
      self._net_conv_channels = cout

    # rpn
    self.rpn_net = nn.Conv2d(self._net_conv_channels, cfg.RPN_CHANNELS, [3, 3], padding=1)

    self.rpn_cls_score_net = nn.Conv2d(cfg.RPN_CHANNELS, self._num_anchors * 2, [1, 1])
    
    self.rpn_bbox_pred_net = nn.Conv2d(cfg.RPN_CHANNELS, self._num_anchors * 4, [1, 1])

    self.cls_score_net = nn.Linear(self._fc7_channels, self._num_classes)
    self.bbox_pred_net = nn.Linear(self._fc7_channels, self._num_classes * 4)
    # added by rgh
    # self.roi_1x1 = nn.Conv2d(self._net_conv_channels, int(self._net_conv_channels/2), [1, 1])
    # self.global_1x1 = nn.Conv2d(self._net_conv_channels, int(self._net_conv_channels/2), [1, 1])
    # self.feat_1x1 = nn.Conv2d(self._net_conv_channels, 492, [1, 1])
    # self.dec_channel = nn.Conv2d(self._net_conv_channels*3, self._net_conv_channels, [1, 1])

    if cfg.ZDF:
      self.global_conv1 = nn.Conv2d(512, 512, 3, stride=2, padding=1)
      self.global_conv2 = nn.Conv2d(512, 512, 3, stride=2, padding=1)
      self.global_conv3 = nn.Conv2d(512, 512, 5, stride=1)
      self.global_upconv1 = nn.ConvTranspose2d(512, 512, 5, stride=1)
      self.global_upconv2 = nn.ConvTranspose2d(512, 512, 3, stride=2, padding=1, output_padding=1)
      self.global_upconv3 = nn.ConvTranspose2d(512, 512, 3, stride=2, padding=1, output_padding=1)
    if cfg.FIX_FEAT:
      # Fix all layers
      for p in self.rpn_net.parameters(): p.requires_grad = False
      for p in self.rpn_cls_score_net.parameters(): p.requires_grad = False
      for p in self.rpn_bbox_pred_net.parameters(): p.requires_grad = False

      for p in self.cls_score_net.parameters(): p.requires_grad = False
      for p in self.bbox_pred_net.parameters(): p.requires_grad = False

    if cfg.SUB_CATEGORY:
      self.cls_sub_score_net = nn.Linear(self._fc7_channels, (self._num_classes-1)*3+1)
    if cfg.DO_PARSING == True:
      # self.mask_deconv1 = nn.ConvTranspose2d(self._net_conv_channels, 256, [2, 2], 2)
      # self.mask_bn0 = nn.BatchNorm2d(256)

      self.mask_conv1 = nn.Conv2d(512, 256, [3, 3], padding=1)
      self.mask_bn1 = nn.BatchNorm2d(256)

      # self.mask_relu = nn.ReLU(inplace=True)

      self.mask_conv2 = nn.Conv2d(256, 256, [3, 3], padding=1)
      self.mask_bn2 = nn.BatchNorm2d(256)

      self.mask_conv3 = nn.Conv2d(256, 256, [3, 3], padding=1)
      self.mask_bn3 = nn.BatchNorm2d(256)

      self.mask_conv4 = nn.Conv2d(256, 256, [3, 3], padding=1)
      self.mask_bn4 = nn.BatchNorm2d(256)

      self.mask_deconv2 = nn.ConvTranspose2d(256, 256, [2, 2], 2)
      self.mask_conv5 = nn.Conv2d(256, self._num_classes, [1, 1])
    self.init_weights()

  def _run_summary_op(self, val=False):
    """
    Run the summary operator: feed the placeholders with corresponding newtork outputs(activations)
    """
    summaries = []
    # Add image gt
    summaries.append(self._add_gt_image_summary())
    # Add event_summaries
    for key, var in self._event_summaries.items():
      summaries.append(tb.summary.scalar(key, var.data[0]))
    self._event_summaries = {}
    if not val:
      # Add score summaries
      # for key, var in self._score_summaries.items():
      #   summaries.append(self._add_score_summary(key, var))
      # self._score_summaries = {}
      # Add act summaries
      for key, var in self._act_summaries.items():
        summaries += self._add_act_summary(key, var)
      self._act_summaries = {}
      # Add train summaries
      for k, var in dict(self.named_parameters()).items():
        if var.requires_grad:
          summaries.append(self._add_train_summary(k, var))

      self._image_gt_summaries = {}
    
    return summaries
  def _gen_pyramid_rois(self, rois, max_h, max_w, scales=[1.5, 2]):

    w = rois[:, 3::4] - rois[:, 1::4] + 1
    h = rois[:, 4::4] - rois[:, 2::4] + 1
    pyramid_rois = []
    for scale in scales:
      x1 = rois[:, 1::4] - (scale - 1) / 2 * w
      x1[x1 < 0] = 0
      y1= rois[:, 2::4] - (scale - 1) / 2 * h
      y1[y1 < 0] = 0
      x2= rois[:, 3::4] + (scale - 1) / 2 * w
      x1[x1 > max_w-1] = max_w-1
      y2= rois[:, 4::4] + (scale - 1) / 2 * h
      y2[y2 > max_h-1] = max_h-1
      newrois = Variable(torch.zeros(rois.size(0), rois.size(1)).cuda(), requires_grad=False)
      #newrois = torch.zeros(rois.size(0), rois.size(1)).cuda()
      newrois[:, 1::4] = x1
      newrois[:, 2::4] = y1
      newrois[:, 3::4] = x2
      newrois[:, 4::4] = y2
      #newrois = Variable(newrois, requires_grad=False)
      pyramid_rois.append(newrois)
    return pyramid_rois
  def _predict(self):
    # This is just _build_network in tf-faster-rcnn
    torch.backends.cudnn.benchmark = False
    net_conv = self._image_to_head() # 1 512 h/16 w/16
    if cfg.LIGHT_RCNN:
      lightrcnn_conv1 = nn.ReLU()(self.lightrcnn_conv1(net_conv))
      lightrcnn_conv2 = nn.ReLU()(self.lightrcnn_conv2(lightrcnn_conv1))

      lightrcnn_conv3 = nn.ReLU()(self.lightrcnn_conv3(net_conv))
      lightrcnn_conv4 = nn.ReLU()(self.lightrcnn_conv4(lightrcnn_conv3))
      net_conv = lightrcnn_conv2 + lightrcnn_conv4
    if cfg.ZDF:
      # TODO:
      unet_down2 = nn.ReLU()(self.global_conv1(net_conv))
      unet_down4 = nn.ReLU()(self.global_conv2(unet_down2))
      unet_hidden = self.global_conv3(unet_down4)
      unet_down4 = nn.ReLU()(self.global_upconv1(unet_hidden)) + unet_down4
      unet_down2 = nn.ReLU()(self.global_upconv2(unet_down4)) + unet_down2
      net_conv = nn.ReLU()(self.global_upconv3(unet_down2)) + net_conv
      #net_conv =
      #nn.ConvTranspose2d

    # build the anchors for the image
    self._anchor_component(net_conv.size(2), net_conv.size(3))

    # 256 5 (1-4是x1 y1 x2 y2 第0维是指这个proposal来自哪个图片，本工程中输入都是一张，该维都是0没用
    rois = self._region_proposal(net_conv)


    if cfg.POOLING_MODE == 'crop':
      pool5 = self._crop_pool_layer(net_conv, rois)
    elif cfg.POOLING_MODE == 'roi':
      pool5 = self._roi_pool_layer(net_conv, rois)
    elif cfg.POOLING_MODE == 'pyramid_crop':
      pool5 = self._crop_pool_layer(net_conv, rois)
      pyramid_rois = self._gen_pyramid_rois(rois, max_h=self._im_info[0], max_w=self._im_info[1])
      for p_rois in pyramid_rois:
        pyramid_pool5 = self._crop_pool_layer(net_conv, p_rois)
        pool5 = torch.cat((pool5, pyramid_pool5), 1)
      pool5 = self.dec_channel(pool5)
    elif cfg.POOLING_MODE == 'pyramid_crop_sum':
      pool5 = self._crop_pool_layer(net_conv, rois)
      pyramid_rois = self._gen_pyramid_rois(rois, max_h=self._im_info[0], max_w=self._im_info[1])
      pool5 = 0.5 * pool5 + \
              0.3 * self._crop_pool_layer(net_conv, pyramid_rois[0]) +\
              0.2 * self._crop_pool_layer(net_conv, pyramid_rois[1])
    elif cfg.POOLING_MODE == 'crop_sum':
        pool5 = self._crop_pool_layer(net_conv, rois)
        global_pool = torch.nn.functional.adaptive_max_pool2d(net_conv, cfg.POOLING_SIZE)
        shape = global_pool.data.shape
        global_pool = global_pool.expand(pool5.data.shape[0], shape[1], shape[2], shape[3])
        pool5 = pool5 + global_pool
    elif cfg.POOLING_MODE == 'crop_cat':
      pool5 = self._crop_pool_layer(net_conv, rois)
      pool5 = self.roi_1x1(pool5)
      global_pool = torch.nn.functional.adaptive_max_pool2d(net_conv, cfg.POOLING_SIZE)
      global_pool = self.global_1x1(global_pool)
      shape = global_pool.data.shape
      global_pool = global_pool.expand(pool5.data.shape[0], shape[1], shape[2], shape[3])
      pool5 = torch.cat((pool5, global_pool), 1)
    elif cfg.POOLING_MODE == 'crop_cat_rgh':

      pool5 = self._crop_pool_layer(net_conv, rois)
      pool5 = self.roi_1x1(pool5)
      shape = net_conv.data.shape # 1 512 h/16 w/16
      # 256 512 h/16 w/16 256个roi对应256个不同的全局feature
      net_conv = net_conv.expand(rois.data.shape[0], shape[1], shape[2], shape[3])
      # global_rois = torch.zeros(rois.size())
      # global_rois[:, 3::4] = (net_conv.size(3)-1) * 16
      # global_rois[:, 4::4] = (net_conv.size(2)-1) * 16
      # global_rois = Variable(global_rois)
      # global_pool = self._crop_pool_layer(net_conv, global_rois)

      mask = torch.ones(net_conv.size()).cuda()# 256 512 h/16 w/16
      rois_np = rois.data.cpu().numpy()
      for i in range(rois.data.shape[0]):
        x1 = min(int(rois_np[i, 1::4] / 16), net_conv.size(3)-1)
        y1 = min(int(rois_np[i, 2::4] / 16), net_conv.size(2)-1)
        x2 = min(int(rois_np[i, 3::4] / 16), net_conv.size(3)-1)
        y2 = min(int(rois_np[i, 4::4] / 16), net_conv.size(2)-1)
        #net_conv[i, :, y1:y2+1, x1:x2+1] = 0
        mask[i, :, y1:y2+1, x1:x2+1] = 0
      mask = Variable(mask)
      net_conv = net_conv*mask
      global_pool = torch.nn.functional.adaptive_max_pool2d(net_conv, cfg.POOLING_SIZE)
      global_pool = self.global_1x1(global_pool)
      pool5 = torch.cat((pool5, global_pool), 1)

    elif cfg.POOLING_MODE == 'roi_cat':
      pool5 = self._roi_pool_layer(net_conv, rois)
      global_pool = torch.nn.functional.adaptive_max_pool2d(net_conv, cfg.POOLING_SIZE)
      shape = global_pool.data.shape
      global_pool = global_pool.expand(pool5.data.shape[0], shape[1], shape[2], shape[3])
      pool5 = torch.cat((pool5, global_pool), 1)

    if cfg.DO_PARSING:
      mask_pool5 = self._crop_pool_layer(net_conv, self._proposal_targets['mask_unit']['mask_rois'], use_for_parsing=True)
      mask_score_map = self._parsing_net(mask_pool5)
      self._predictions["mask_score_map"] = mask_score_map
    if self._mode == 'TRAIN':
      torch.backends.cudnn.benchmark = True # benchmark because now the input size are fixed
    fc7 = self._head_to_tail(pool5)

    cls_prob, bbox_pred = self._region_classification(fc7)
    
    for k in self._predictions.keys():
      self._score_summaries[k] = self._predictions[k]

    return rois, cls_prob, bbox_pred

  def forward(self, image, im_info, gt_boxes=None, parsing_labels=None, mode='TRAIN'):
    self._image_gt_summaries['image'] = image
    self._image_gt_summaries['gt_boxes'] = gt_boxes
    self._image_gt_summaries['im_info'] = im_info
    self._image = Variable(torch.from_numpy(image.transpose([0,3,1,2])).cuda(), volatile=mode == 'TEST')
    self._parsing_labels = Variable(torch.from_numpy(parsing_labels).cuda(), volatile=mode == 'TEST')if parsing_labels is not None else None
    self._im_info = im_info # No need to change; actually it can be an list
    self._gt_boxes = Variable(torch.from_numpy(gt_boxes).cuda()) if gt_boxes is not None else None
    self._mode = mode

    rois, cls_prob, bbox_pred = self._predict()

    if mode == 'TEST':
      stds = bbox_pred.data.new(cfg.TRAIN.BBOX_NORMALIZE_STDS).repeat(self._num_classes).unsqueeze(0).expand_as(bbox_pred)
      means = bbox_pred.data.new(cfg.TRAIN.BBOX_NORMALIZE_MEANS).repeat(self._num_classes).unsqueeze(0).expand_as(bbox_pred)
      self._predictions["bbox_pred"] = bbox_pred.mul(Variable(stds)).add(Variable(means))
    else:
      self._add_losses() # compute losses

  def init_weights(self):
    # def normal_init(m, mean, stddev, truncated=False):
    #   """
    #   weight initalizer: truncated normal and random normal.
    #   """
    #   # x is a parameter
    #   if truncated:
    #     m.weight.data.normal_().fmod_(2).mul_(stddev).add_(mean) # not a perfect approximation
    #   else:
    #     m.weight.data.normal_(mean, stddev)
    #   m.bias.data.zero_()
    # normal_init(self.rpn_net, 0, 0.01, cfg.TRAIN.TRUNCATED)
    # normal_init(self.rpn_cls_score_net, 0, 0.01, cfg.TRAIN.TRUNCATED)
    # normal_init(self.rpn_bbox_pred_net, 0, 0.01, cfg.TRAIN.TRUNCATED)
    # normal_init(self.cls_score_net, 0, 0.01, cfg.TRAIN.TRUNCATED)
    # normal_init(self.bbox_pred_net, 0, 0.001, cfg.TRAIN.TRUNCATED)
    # # added by rgh
    # normal_init(self.roi_1x1, 0, 0.01, cfg.TRAIN.TRUNCATED)
    # normal_init(self.global_1x1, 0, 0.01, cfg.TRAIN.TRUNCATED)
    # normal_init(self.feat_1x1, 0, 0.01, cfg.TRAIN.TRUNCATED)
    # normal_init(self.dec_channel, 0, 0.01, cfg.TRAIN.TRUNCATED)
    # if cfg.ZDF:
    #   if cfg.ZDF_GAUSSIAN:
    #     normal_init(self.global_conv1, 0, 0.01, cfg.TRAIN.TRUNCATED)
    #     normal_init(self.global_conv2, 0, 0.01, cfg.TRAIN.TRUNCATED)
    #     normal_init(self.global_conv3, 0, 0.01, cfg.TRAIN.TRUNCATED)
    #     normal_init(self.global_upconv1, 0, 0.01, cfg.TRAIN.TRUNCATED)
    #     normal_init(self.global_upconv2, 0, 0.01, cfg.TRAIN.TRUNCATED)
    #     normal_init(self.global_upconv3, 0, 0.01, cfg.TRAIN.TRUNCATED)
    # if cfg.SUB_CATEGORY:
    #   normal_init(self.cls_sub_score_net, 0, 0.01, cfg.TRAIN.TRUNCATED)

    ####  kaiming init
    # for m in self.modules():
    #   if isinstance(m, nn.Conv2d):
    #     n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
    #     m.weight.data.normal_(0, math.sqrt(2. / n))
    #     if m.bias is not None:
    #       m.bias.data.zero_()
    #   elif isinstance(m, nn.BatchNorm2d):
    #     m.weight.data.fill_(1)
    #     m.bias.data.zero_()
    #   elif isinstance(m, nn.Linear):
    #     n = m.weight.size(1)
    #     m.weight.data.normal_(0, 0.01)
    #     m.bias.data.zero_()

    ### gaussian init
    for m in self.modules():
      # classname = m.__class__.__name__  : Conv2d  ConvTranspose2d  if classname.find('Conv') != -1:
      if isinstance(m, nn.Conv2d):
        m.weight.data.normal_(0, 0.01)
        if m.bias is not None:
          m.bias.data.zero_()
      elif isinstance(m, nn.ConvTranspose2d):
        m.weight.data.normal_(0, 0.01)
        if m.bias is not None:
          m.bias.data.zero_()
      elif isinstance(m, nn.BatchNorm2d):
        m.weight.data.fill_(1)
        m.bias.data.zero_()
      elif isinstance(m, nn.Linear):
        n = m.weight.size(1)
        m.weight.data.normal_(0, 0.01)
        m.bias.data.zero_()
    self.bbox_pred_net.weight.data.normal_(0, 0.001)
  # Extract the head feature maps, for example for vgg16 it is conv5_3
  # only useful during testing mode
  def extract_head(self, image):
    feat = self._layers["head"](Variable(torch.from_numpy(image.transpose([0,3,1,2])).cuda(), volatile=True))
    return feat

  # only useful during testing mode
  def test_image(self, image, im_info, parsing_labels=None):
    self.eval()
    self.forward(image, im_info, gt_boxes=None, parsing_labels=parsing_labels, mode='TEST')
    cls_score, cls_prob, bbox_pred, rois = self._predictions["cls_score"].data.cpu().numpy(), \
                                                     self._predictions['cls_prob'].data.cpu().numpy(), \
                                                     self._predictions['bbox_pred'].data.cpu().numpy(), \
                                                     self._predictions['rois'].data.cpu().numpy()
    if cfg.DO_PARSING:
      mask_score_map = self._predictions['mask_score_map']
      mask_score_map = torch.round(mask_score_map)
      #mask_score_map_sigmoid = F.sigmoid(mask_score_map)
      return cls_score, cls_prob, bbox_pred, rois, mask_score_map.data.cpu().numpy()
    return cls_score, cls_prob, bbox_pred, rois

  def delete_intermediate_states(self):
    # Delete intermediate result to save memory
    for d in [self._losses, self._predictions, self._anchor_targets, self._proposal_targets]:
      for k in list(d):
        del d[k]

  def get_summary(self, blobs):
    self.eval()
    self.forward(blobs['data'], blobs['im_info'], blobs['gt_boxes'], blobs['parsing_labels'])
    self.train()
    summary = self._run_summary_op(True)

    return summary

  def train_step(self, blobs, train_op):
    self.forward(blobs['data'], blobs['im_info'], blobs['gt_boxes'], blobs['parsing_labels'])
    rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss = self._losses["rpn_cross_entropy"].data[0], \
                                                                        self._losses['rpn_loss_box'].data[0], \
                                                                        self._losses['cross_entropy'].data[0], \
                                                                        self._losses['loss_box'].data[0], \
                                                                        self._losses['total_loss'].data[0]
    #utils.timer.timer.tic('backward')
    train_op.zero_grad()
    self._losses['total_loss'].backward()
    #utils.timer.timer.toc('backward')
    train_op.step()

    self.delete_intermediate_states()

    return rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss

  def train_step_with_summary(self, blobs, train_op):
    self.forward(blobs['data'], blobs['im_info'], blobs['gt_boxes'], blobs['parsing_labels'])
    rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss = self._losses["rpn_cross_entropy"].data[0], \
                                                                        self._losses['rpn_loss_box'].data[0], \
                                                                        self._losses['cross_entropy'].data[0], \
                                                                        self._losses['loss_box'].data[0], \
                                                                        self._losses['total_loss'].data[0]
    train_op.zero_grad()
    self._losses['total_loss'].backward()
    train_op.step()
    summary = self._run_summary_op()

    self.delete_intermediate_states()

    return rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss, summary

  def train_step_no_return(self, blobs, train_op):
    self.forward(blobs['data'], blobs['im_info'], blobs['gt_boxes'])
    train_op.zero_grad()
    self._losses['total_loss'].backward()
    train_op.step()
    self.delete_intermediate_states()

  def load_state_dict(self, state_dict):
    """
    Because we remove the definition of fc layer in resnet now, it will fail when loading 
    the model trained before.
    To provide back compatibility, we overwrite the load_state_dict
    """
    print('network load_state_dict')
    nn.Module.load_state_dict(self, {k: state_dict[k] for k in list(self.state_dict())})
    #print('network')
    # only copy common items and has common shape
    # model_dict = self.state_dict()
    # state_dict = {k: v for k, v in state_dict.items() if k in model_dict and v.shape == model_dict[k].shape}
    # model_dict.update(state_dict)
    # nn.Module.load_state_dict(self, model_dict)
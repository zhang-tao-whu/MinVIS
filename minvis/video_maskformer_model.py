# Copyright (c) 2021-2022, NVIDIA Corporation & Affiliates. All rights reserved.
#
# This work is made available under the Nvidia Source Code License-NC.
# To view a copy of this license, visit
# https://github.com/NVlabs/MinVIS/blob/main/LICENSE

# Copyright (c) Facebook, Inc. and its affiliates.
import logging
import math
from typing import Tuple
import einops

import torch
from torch import nn
from torch.nn import functional as F

from detectron2.config import configurable
from detectron2.data import MetadataCatalog
from detectron2.modeling import META_ARCH_REGISTRY, build_backbone, build_sem_seg_head
from detectron2.modeling.backbone import Backbone
from detectron2.modeling.postprocessing import sem_seg_postprocess
from detectron2.structures import Boxes, ImageList, Instances, BitMasks

from mask2former_video.modeling.criterion import VideoSetCriterion, VideoSetCriterion_
from mask2former_video.modeling.matcher import VideoHungarianMatcher, VideoHungarianMatcher_
from mask2former_video.utils.memory import retry_if_cuda_oom

from scipy.optimize import linear_sum_assignment

from mask2former_video.modeling.transformer_decoder.video_mask2former_transformer_decoder import SelfAttentionLayer, CrossAttentionLayer, FFNLayer, MLP

logger = logging.getLogger(__name__)


@META_ARCH_REGISTRY.register()
class VideoMaskFormer_frame(nn.Module):
    """
    Main class for mask classification semantic segmentation architectures.
    """

    @configurable
    def __init__(
        self,
        *,
        backbone: Backbone,
        sem_seg_head: nn.Module,
        criterion: nn.Module,
        num_queries: int,
        object_mask_threshold: float,
        overlap_threshold: float,
        metadata,
        size_divisibility: int,
        sem_seg_postprocess_before_inference: bool,
        pixel_mean: Tuple[float],
        pixel_std: Tuple[float],
        # video
        num_frames,
        window_inference,
    ):
        """
        Args:
            backbone: a backbone module, must follow detectron2's backbone interface
            sem_seg_head: a module that predicts semantic segmentation from backbone features
            criterion: a module that defines the loss
            num_queries: int, number of queries
            object_mask_threshold: float, threshold to filter query based on classification score
                for panoptic segmentation inference
            overlap_threshold: overlap threshold used in general inference for panoptic segmentation
            metadata: dataset meta, get `thing` and `stuff` category names for panoptic
                segmentation inference
            size_divisibility: Some backbones require the input height and width to be divisible by a
                specific integer. We can use this to override such requirement.
            sem_seg_postprocess_before_inference: whether to resize the prediction back
                to original input size before semantic segmentation inference or after.
                For high-resolution dataset like Mapillary, resizing predictions before
                inference will cause OOM error.
            pixel_mean, pixel_std: list or tuple with #channels element, representing
                the per-channel mean and std to be used to normalize the input image
            semantic_on: bool, whether to output semantic segmentation prediction
            instance_on: bool, whether to output instance segmentation prediction
            panoptic_on: bool, whether to output panoptic segmentation prediction
            test_topk_per_image: int, instance segmentation parameter, keep topk instances per image
        """
        super().__init__()
        self.backbone = backbone
        self.sem_seg_head = sem_seg_head
        self.criterion = criterion
        self.num_queries = num_queries
        self.overlap_threshold = overlap_threshold
        self.object_mask_threshold = object_mask_threshold
        self.metadata = metadata
        if size_divisibility < 0:
            # use backbone size_divisibility if not set
            size_divisibility = self.backbone.size_divisibility
        self.size_divisibility = size_divisibility
        self.sem_seg_postprocess_before_inference = sem_seg_postprocess_before_inference
        self.register_buffer("pixel_mean", torch.Tensor(pixel_mean).view(-1, 1, 1), False)
        self.register_buffer("pixel_std", torch.Tensor(pixel_std).view(-1, 1, 1), False)

        self.num_frames = num_frames
        self.window_inference = window_inference

        self.tracker = QueryTracker(num_object_query=100,
                                    hidden_channel=256,
                                    feedforward_channel=2048,
                                    num_head=8,
                                    decoder_layer_num=6,
                                    mask_dim=256,
                                    class_num=25,
                                    detach_frame_connection=True)
        #self.embed_proj = nn.Linear(256, 256)

    @classmethod
    def from_config(cls, cfg):
        backbone = build_backbone(cfg)
        sem_seg_head = build_sem_seg_head(cfg, backbone.output_shape())

        # Loss parameters:
        deep_supervision = cfg.MODEL.MASK_FORMER.DEEP_SUPERVISION
        no_object_weight = cfg.MODEL.MASK_FORMER.NO_OBJECT_WEIGHT

        # loss weights
        class_weight = cfg.MODEL.MASK_FORMER.CLASS_WEIGHT
        dice_weight = cfg.MODEL.MASK_FORMER.DICE_WEIGHT
        mask_weight = cfg.MODEL.MASK_FORMER.MASK_WEIGHT
        contrast_weight = cfg.MODEL.MASK_FORMER.CONTRAST_WEIGHT

        # building criterion
        matcher = VideoHungarianMatcher_(
            cost_class=class_weight,
            cost_mask=mask_weight,
            cost_dice=dice_weight,
            num_points=cfg.MODEL.MASK_FORMER.TRAIN_NUM_POINTS,
        )

        weight_dict = {"loss_ce": class_weight, "loss_mask": mask_weight, "loss_dice": dice_weight,
                       "loss_contrast": contrast_weight}

        if deep_supervision:
            dec_layers = cfg.MODEL.MASK_FORMER.DEC_LAYERS
            aux_weight_dict = {}
            for i in range(dec_layers - 1):
                aux_weight_dict.update({k + f"_{i}": v for k, v in weight_dict.items()})
            weight_dict.update(aux_weight_dict)

        losses = ["labels", "masks", "contrast"]

        criterion = VideoSetCriterion_(
            sem_seg_head.num_classes,
            matcher=matcher,
            weight_dict=weight_dict,
            eos_coef=no_object_weight,
            losses=losses,
            num_points=cfg.MODEL.MASK_FORMER.TRAIN_NUM_POINTS,
            oversample_ratio=cfg.MODEL.MASK_FORMER.OVERSAMPLE_RATIO,
            importance_sample_ratio=cfg.MODEL.MASK_FORMER.IMPORTANCE_SAMPLE_RATIO,
        )

        return {
            "backbone": backbone,
            "sem_seg_head": sem_seg_head,
            "criterion": criterion,
            "num_queries": cfg.MODEL.MASK_FORMER.NUM_OBJECT_QUERIES,
            "object_mask_threshold": cfg.MODEL.MASK_FORMER.TEST.OBJECT_MASK_THRESHOLD,
            "overlap_threshold": cfg.MODEL.MASK_FORMER.TEST.OVERLAP_THRESHOLD,
            "metadata": MetadataCatalog.get(cfg.DATASETS.TRAIN[0]),
            "size_divisibility": cfg.MODEL.MASK_FORMER.SIZE_DIVISIBILITY,
            "sem_seg_postprocess_before_inference": True,
            "pixel_mean": cfg.MODEL.PIXEL_MEAN,
            "pixel_std": cfg.MODEL.PIXEL_STD,
            # video
            "num_frames": cfg.INPUT.SAMPLING_FRAME_NUM,
            "window_inference": cfg.MODEL.MASK_FORMER.TEST.WINDOW_INFERENCE
        }

    @property
    def device(self):
        return self.pixel_mean.device

    def forward(self, batched_inputs):
        """
        Args:
            batched_inputs: a list, batched outputs of :class:`DatasetMapper`.
                Each item in the list contains the inputs for one image.
                For now, each item in the list is a dict that contains:
                   * "image": Tensor, image in (C, H, W) format.
                   * "instances": per-region ground truth
                   * Other information that's included in the original dicts, such as:
                     "height", "width" (int): the output resolution of the model (may be different
                     from input resolution), used in inference.
        Returns:
            list[dict]:
                each dict has the results for one image. The dict contains the following keys:

                * "sem_seg":
                    A Tensor that represents the
                    per-pixel segmentation prediced by the head.
                    The prediction has shape KxHxW that represents the logits of
                    each class for each pixel.
                * "panoptic_seg":
                    A tuple that represent panoptic output
                    panoptic_seg (Tensor): of shape (height, width) where the values are ids for each segment.
                    segments_info (list[dict]): Describe each segment in `panoptic_seg`.
                        Each dict contains keys "id", "category_id", "isthing".
        """
        images = []
        for video in batched_inputs:
            for frame in video["image"]:
                images.append(frame.to(self.device))
        images = [(x - self.pixel_mean) / self.pixel_std for x in images]
        images = ImageList.from_tensors(images, self.size_divisibility)

        if not self.training and self.window_inference:
            outputs = self.run_window_inference(images.tensor, window_size=3)
            # frame_embds = outputs['pred_embds']  # b c t q
            # mask_features = outputs['mask_features']
            # outputs = self.tracker(frame_embds, mask_features)
        else:
            self.backbone.eval()
            self.sem_seg_head.eval()
            with torch.no_grad():
                features = self.backbone(images.tensor)
                outputs = self.sem_seg_head(features)
                frame_embds = outputs['pred_embds'].clone().detach()  # b c t q
                mask_features = outputs['mask_features'].clone().detach().unsqueeze(0)
                del outputs['pred_embds']
                del outputs['mask_features']
                del outputs
                torch.cuda.empty_cache()
            outputs = self.tracker(frame_embds, mask_features)

        # pred_logits (bs, nq, t, c)
        # pred_masks (bs, nq, t, h, w)
        # pred_embds (b c t q)

        if self.training:
            # mask classification target
            targets = self.prepare_targets(batched_inputs, images)

            outputs, targets = self.frame_decoder_loss_reshape(outputs, targets)

            # bipartite matching-based loss
            #losses = self.criterion(outputs, targets)
            losses = self.criterion(outputs, targets, use_contrast=False)

            for k in list(losses.keys()):
                if k in self.criterion.weight_dict:
                    losses[k] *= self.criterion.weight_dict[k]
                else:
                    # remove this loss if not specified in `weight_dict`
                    losses.pop(k)
            return losses
        else:
            #outputs = self.post_processing(outputs)
            # pred_logits (bs, nq, t, c)
            # pred_masks (bs, nq, t, h, w)
            # pred_embds (b c t q)
            outputs = self.post_processing_(outputs)

            mask_cls_results = outputs["pred_logits"]
            mask_pred_results = outputs["pred_masks"]

            mask_cls_result = mask_cls_results[0]
            mask_pred_result = mask_pred_results[0]
            first_resize_size = (images.tensor.shape[-2], images.tensor.shape[-1])

            input_per_image = batched_inputs[0]
            image_size = images.image_sizes[0]  # image size without padding after data augmentation

            height = input_per_image.get("height", image_size[0])  # raw image size before data augmentation
            width = input_per_image.get("width", image_size[1])

            return retry_if_cuda_oom(self.inference_video)(mask_cls_result, mask_pred_result, image_size, height, width, first_resize_size)

    def frame_decoder_loss_reshape(self, outputs, targets):
        # pred_logits (bs, nq, t, c)
        # pred_masks (bs, nq, t, h, w)
        #outputs['pred_masks'] = einops.rearrange(outputs['pred_masks'], 'b q t h w -> b q t h w')
        outputs['pred_logits'] = einops.rearrange(outputs['pred_logits'], 'b q t c -> b q t c')
        if 'aux_outputs' in outputs:
            for i in range(len(outputs['aux_outputs'])):
                #outputs['aux_outputs'][i]['pred_masks'] = einops.rearrange(
                #    outputs['aux_outputs'][i]['pred_masks'], 'b q t h w -> (b t) q () h w'
                #)
                outputs['aux_outputs'][i]['pred_logits'] = einops.rearrange(
                    outputs['aux_outputs'][i]['pred_logits'], 'b q t c -> b q t c'
                )

        gt_instances = []
        for targets_per_video in targets:
            # labels: N (num instances)
            # ids: N, num_labeled_frames
            # masks: N, num_labeled_frames, H, W

            #num_labeled_frames = targets_per_video['ids'].shape[1]
            # for f in range(num_labeled_frames):
            #     labels = targets_per_video['labels']
            #     ids = targets_per_video['ids'][:, [f]]
            #     masks = targets_per_video['masks'][:, [f], :, :]
            #     gt_instances.append({"labels": labels, "ids": ids, "masks": masks})
            labels = targets_per_video['labels'] # (N, )
            ids = targets_per_video['ids'] # (N, T)
            masks = targets_per_video['masks'] # (N, T, H, W)
            gt_instances.append({"labels": labels, "ids": ids, "masks": masks})
        # outputs -> {'masks': (bt, q, h, w), 'logits': (bt, 1, c)}
        # gt_instances -> [per image gt * bt], per image gt -> {'labels': (N, ), 'ids': (N, ), 'masks': (N, H, W)}
        return outputs, gt_instances

    def match_from_embds(self, tgt_embds, cur_embds):

        cur_embds = cur_embds / cur_embds.norm(dim=1)[:, None]
        tgt_embds = tgt_embds / tgt_embds.norm(dim=1)[:, None]
        cos_sim = torch.mm(cur_embds, tgt_embds.transpose(0,1))

        cost_embd = 1 - cos_sim

        C = 1.0 * cost_embd
        C = C.cpu()

        indices = linear_sum_assignment(C.transpose(0, 1))  # target x current
        indices = indices[1]  # permutation that makes current aligns to target

        return indices

    def match_from_embds_(self, tgt_embds, cur_embds, scores=None):

        cur_embds = cur_embds / cur_embds.norm(dim=1)[:, None]
        tgt_embds = [tgt_embd / tgt_embd.norm(dim=1)[:, None] for tgt_embd in tgt_embds]
        C = 0
        weights = [0.1, 0.3, 0.6]
        #weights = [0.05, 0.15, 0.8]
        for i, (weight, tgt_embd) in enumerate(zip(weights, tgt_embds)):
            cos_sim = torch.mm(cur_embds, tgt_embd.transpose(0,1))
            cost_embd = 1 - cos_sim
            if scores is None:
                C = C + cost_embd * weight
            else:
                C = C + cost_embd * scores[i].unsqueeze(0) * weight

        if scores is not None:
            score_average = torch.stack(scores, dim=0).sum(dim=0)
            C = C / (score_average + 1e-6).unsqueeze(0)
        C = C.cpu()

        indices = linear_sum_assignment(C.transpose(0, 1))  # target x current
        indices = indices[1]  # permutation that makes current aligns to target

        return indices

    def post_processing(self, outputs):
        pred_logits, pred_masks, pred_embds = outputs['pred_logits'], outputs['pred_masks'], outputs['pred_embds']

        # pred_logits: 1 t q c
        # pred_masks: 1 q t h w
        pred_logits = pred_logits[0]
        pred_masks = einops.rearrange(pred_masks[0], 'q t h w -> t q h w')
        pred_embds = einops.rearrange(pred_embds[0], 'c t q -> t q c')

        pred_logits = list(torch.unbind(pred_logits))
        pred_masks = list(torch.unbind(pred_masks))
        pred_embds = list(torch.unbind(pred_embds))

        out_logits = []
        out_masks = []
        out_embds = []
        out_logits.append(pred_logits[0])
        out_masks.append(pred_masks[0])
        out_embds.append(pred_embds[0])

        for i in range(1, len(pred_logits)):
            indices = self.match_from_embds(out_embds[-1], pred_embds[i])

            out_logits.append(pred_logits[i][indices, :])
            out_masks.append(pred_masks[i][indices, :, :])
            out_embds.append(pred_embds[i][indices, :])

        out_logits = sum(out_logits)/len(out_logits)
        out_masks = torch.stack(out_masks, dim=1)  # q h w -> q t h w

        out_logits = out_logits.unsqueeze(0)
        out_masks = out_masks.unsqueeze(0)

        outputs['pred_logits'] = out_logits
        outputs['pred_masks'] = out_masks

        return outputs

    def post_processing_(self, outputs):
        pred_logits, pred_masks, pred_embds = outputs['pred_logits'].transpose(1, 2), outputs['pred_masks'], outputs['pred_embds']
        # pred_logits: 1 t q c
        # pred_masks: 1 q t h w
        # pred_embeds: 1 c t q
        pred_logits = pred_logits[0]
        pred_scores = torch.max(F.softmax(pred_logits, dim=-1)[..., :-1], dim=-1)[0]
        pred_masks = einops.rearrange(pred_masks[0], 'q t h w -> t q h w')
        pred_embds = einops.rearrange(pred_embds[0], 'c t q -> t q c')

        pred_logits = list(torch.unbind(pred_logits))
        pred_masks = list(torch.unbind(pred_masks))
        pred_embds = list(torch.unbind(pred_embds))

        out_logits = []
        out_masks = []
        out_embds = []
        out_scores = []
        out_logits.append(pred_logits[0])
        out_masks.append(pred_masks[0])
        out_embds += [pred_embds[0]] * 3
        out_scores += [pred_scores[0]] * 3

        for i in range(1, len(pred_logits)):
            # indices = self.match_from_embds_(out_embds[-3:], pred_embds[i], scores=out_scores[-3:])
            indices = self.match_from_embds_(out_embds[-3:], pred_embds[i])

            out_logits.append(pred_logits[i][indices, :])
            out_masks.append(pred_masks[i][indices, :, :])
            out_embds.append(pred_embds[i][indices, :])
            out_scores.append(pred_scores[i][indices])

        out_logits = sum(out_logits)/len(out_logits)
        out_masks = torch.stack(out_masks, dim=1)  # q h w -> q t h w

        out_logits = out_logits.unsqueeze(0)
        out_masks = out_masks.unsqueeze(0)

        outputs['pred_logits'] = out_logits
        outputs['pred_masks'] = out_masks

        return outputs

    def run_window_inference(self, images_tensor, window_size=30):

        # self.backbone.eval()
        # self.sem_seg_head.eval()
        # with torch.no_grad():
        #     features = self.backbone(images.tensor)
        #     outputs = self.sem_seg_head(features)
        #     frame_embds = outputs['pred_embds'].clone().detach()  # b c t q
        #     mask_features = outputs['mask_features'].clone().detach()
        #     del outputs['pred_embds']
        #     del outputs['mask_features']
        #     del outputs
        #     torch.cuda.empty_cache()
        # outputs = self.tracker(frame_embds, mask_features)

        iters = len(images_tensor) // window_size
        if len(images_tensor) % window_size != 0:
            iters += 1
        out_list = []
        for i in range(iters):
            start_idx = i * window_size
            end_idx = (i+1) * window_size

            features = self.backbone(images_tensor[start_idx:end_idx])
            out = self.sem_seg_head(features)
            frame_embds = out['pred_embds']  # b c t q
            mask_features = out['mask_features'].unsqueeze(0)
            if i == 0:
                track_out = self.tracker(frame_embds, mask_features)
            else:
                track_out = self.tracker(frame_embds, mask_features,
                                         init_query=out_list[-1]['pred_embds'].permute(2, 3, 0, 1)[-1])

            del features['res2'], features['res3'], features['res4'], features['res5']
            del mask_features
            for j in range(len(track_out['aux_outputs'])):
                del track_out['aux_outputs'][j]['pred_masks'], track_out['aux_outputs'][j]['pred_logits']
            out_list.append(track_out)

        # pred_logits (bs, nq, t, c)
        # pred_masks (bs, nq, t, h, w)
        # pred_embds (b c t q)
        # merge outputs
        outputs = {}
        outputs['pred_logits'] = torch.cat([x['pred_logits'] for x in out_list], dim=2).detach()
        outputs['pred_masks'] = torch.cat([x['pred_masks'] for x in out_list], dim=2).detach()
        outputs['pred_embds'] = torch.cat([x['pred_embds'] for x in out_list], dim=2).detach()
        return outputs

    def prepare_targets(self, targets, images):
        h_pad, w_pad = images.tensor.shape[-2:]
        gt_instances = []
        for targets_per_video in targets:
            _num_instance = len(targets_per_video["instances"][0]) # targets_per_video["instances"] -> [per frame [per instance id]]
            mask_shape = [_num_instance, self.num_frames, h_pad, w_pad]
            gt_masks_per_video = torch.zeros(mask_shape, dtype=torch.bool, device=self.device)

            gt_ids_per_video = []
            for f_i, targets_per_frame in enumerate(targets_per_video["instances"]):
                targets_per_frame = targets_per_frame.to(self.device)
                h, w = targets_per_frame.image_size

                gt_ids_per_video.append(targets_per_frame.gt_ids[:, None])
                gt_masks_per_video[:, f_i, :h, :w] = targets_per_frame.gt_masks.tensor

            gt_ids_per_video = torch.cat(gt_ids_per_video, dim=1) #(n_inst, frame)
            valid_idx = (gt_ids_per_video != -1).any(dim=-1) # filter which instance in any frames

            gt_classes_per_video = targets_per_frame.gt_classes[valid_idx]          # N,
            gt_ids_per_video = gt_ids_per_video[valid_idx]                          # N, num_frames, i instance not in j frame, id[i, j] = -1
            gt_instances.append({"labels": gt_classes_per_video, "ids": gt_ids_per_video})
            gt_masks_per_video = gt_masks_per_video[valid_idx].float()          # N, num_frames, H, W
            gt_instances[-1].update({"masks": gt_masks_per_video})
        #gt_instances -> [per video instance], per video instance {'labels': (N, ), 'ids': (N, f), 'masks': (N, f, H, W)}
        return gt_instances

    def inference_video(self, pred_cls, pred_masks, img_size, output_height, output_width, first_resize_size, max_num=20):
        # pred_cls (nq, t, c)
        # pred_masks (nq, t, h, w)
        #pred_cls = torch.mean(pred_cls, dim=1) # (nq, c)
        if len(pred_cls) > 0:
            scores = F.softmax(pred_cls, dim=-1)[:, :-1]
            labels = torch.arange(self.sem_seg_head.num_classes, device=self.device).unsqueeze(0).repeat(self.num_queries, 1).flatten(0, 1)
            # keep top-10 predictions
            scores_per_image, topk_indices = scores.flatten(0, 1).topk(max_num, sorted=False)
            labels_per_image = labels[topk_indices]
            topk_indices = topk_indices // self.sem_seg_head.num_classes
            pred_masks = pred_masks[topk_indices]

            pred_masks = F.interpolate(
                pred_masks, size=first_resize_size, mode="bilinear", align_corners=False
            )

            pred_masks = pred_masks[:, :, : img_size[0], : img_size[1]]
            pred_masks = F.interpolate(
                pred_masks, size=(output_height, output_width), mode="bilinear", align_corners=False
            )

            masks = pred_masks > 0.

            out_scores = scores_per_image.tolist()
            out_labels = labels_per_image.tolist()
            out_masks = [m for m in masks.cpu()]
        else:
            out_scores = []
            out_labels = []
            out_masks = []

        video_output = {
            "image_size": (output_height, output_width),
            "pred_scores": out_scores,
            "pred_labels": out_labels,
            "pred_masks": out_masks,
        }

        return video_output

class QueryTracker(torch.nn.Module):
    def __init__(self,
                 num_object_query=30,
                 hidden_channel=256,
                 feedforward_channel=2048,
                 num_head=8,
                 decoder_layer_num=6,
                 mask_dim=256,
                 class_num=25,
                 detach_frame_connection=False):
        super(QueryTracker, self).__init__()
        self.detach_frame_connection = detach_frame_connection

        # init for object query
        self.num_object_query = num_object_query
        # learnable query features
        self.query_feat = nn.Embedding(num_object_query, hidden_channel)
        # learnable query p.e.
        self.query_embed = nn.Embedding(num_object_query, hidden_channel)

        self.frame_pos_embed = nn.Embedding(100, hidden_channel)

        # init transformer layers
        self.num_heads = num_head
        self.num_layers = decoder_layer_num
        self.transformer_self_attention_layers = nn.ModuleList()
        self.transformer_cross_attention_layers = nn.ModuleList()
        self.transformer_ffn_layers = nn.ModuleList()

        for _ in range(self.num_layers):
            self.transformer_self_attention_layers.append(
                SelfAttentionLayer(
                    d_model=hidden_channel,
                    nhead=num_head,
                    dropout=0.0,
                    normalize_before=False,
                )
            )

            self.transformer_cross_attention_layers.append(
                CrossAttentionLayer(
                    d_model=hidden_channel,
                    nhead=num_head,
                    dropout=0.0,
                    normalize_before=False,
                )
            )

            self.transformer_ffn_layers.append(
                FFNLayer(
                    d_model=hidden_channel,
                    dim_feedforward=feedforward_channel,
                    dropout=0.0,
                    normalize_before=False,
                )
            )

        self.decoder_norm = nn.LayerNorm(hidden_channel)

        # init heads
        self.class_embed = nn.Linear(hidden_channel, class_num + 1)
        self.mask_embed = MLP(hidden_channel, hidden_channel, mask_dim, 3)

        self.mask_feature_proj = nn.Conv2d(
            mask_dim,
            mask_dim,
            kernel_size=1,
            stride=1,
            padding=0,
        )

        self.frame_proj = nn.Linear(hidden_channel, hidden_channel)

    def forward(self, frame_embeds, mask_features, init_query=None):
        mask_features_shape = mask_features.shape
        mask_features = self.mask_feature_proj(mask_features.flatten(0, 1)).reshape(*mask_features_shape)
        # init_query (q, b, c)
        frame_embeds = frame_embeds.permute(2, 3, 0, 1)  # t, q, b, c
        n_frame, n_q, bs, _ = frame_embeds.size()
        outputs = []
        if init_query is None:
            output = self.query_feat.weight.unsqueeze(1).repeat(1, bs, 1) # q, b, c
        else:
            output = self.frame_proj(init_query)

        output_pos = self.query_embed.weight.unsqueeze(1).repeat(1, bs, 1) # q, b, c

        frame_pos_embed = self.frame_pos_embed.weight.unsqueeze(1).repeat(1, bs, 1)

        for i in range(n_frame):
            single_frame_embeds = frame_embeds[i]
            ms_output = []
            for j in range(self.num_layers):
                output = self.transformer_cross_attention_layers[j](
                    output, single_frame_embeds,
                    memory_mask=None,
                    memory_key_padding_mask=None,  # here we do not apply masking on padded region
                    pos=frame_pos_embed, query_pos=output_pos
                )

                output = self.transformer_self_attention_layers[j](
                    output, tgt_mask=None,
                    tgt_key_padding_mask=None,
                    query_pos=output_pos
                )

                # FFN
                output = self.transformer_ffn_layers[j](
                    output
                )
                ms_output.append(output)
            if self.detach_frame_connection:
                output = output.detach()
            output = self.frame_proj(output)
            ms_output = torch.stack(ms_output, dim=0)
            outputs.append(ms_output)
        outputs = torch.stack(outputs, dim=0)  # frame, decoder_layer, q, b, c

        outputs_class, outputs_masks = self.prediction(outputs, mask_features)

        out = {
           'pred_logits': outputs_class[-1],
           'pred_masks': outputs_masks[-1],
           'aux_outputs': self._set_aux_loss(
               outputs_class, outputs_masks
           ),
           'pred_embds': outputs[:, -1].permute(2, 3, 0, 1)  # b c t q
        }
        # pred_logits (bs, nq, t, c)
        # pred_masks (bs, nq, t, h, w)
        return out

    @torch.jit.unused
    def _set_aux_loss(self, outputs_class, outputs_seg_masks):
        # this is a workaround to make torchscript happy, as torchscript
        # doesn't support dictionary with non-homogeneous values, such
        # as a dict having both a Tensor and a list.
        return [{"pred_logits": a, "pred_masks": b}
                for a, b in zip(outputs_class[:-1], outputs_seg_masks[:-1])
                ]

    def prediction(self, outputs, mask_features):
        # outputs (T, L, q, b, c)
        # mask_features (b, T, C, H, W)
        decoder_output = self.decoder_norm(outputs)
        decoder_output = decoder_output.permute(1, 3, 0, 2, 4)  # (L, B, T, q, C)
        outputs_class = self.class_embed(decoder_output).transpose(2, 3) # (L, B, q, T, Cls+1)
        mask_embed = self.mask_embed(decoder_output)
        outputs_mask = torch.einsum("lbtqc,btchw->lbqthw", mask_embed, mask_features)
        return outputs_class, outputs_mask






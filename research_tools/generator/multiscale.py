import numpy as np
import torch
import json
from tqdm.auto import tqdm
from copy import deepcopy

from yolox.utils import postprocess
from yolox.utils.dist import get_local_rank, get_world_size, wait_for_the_master

from .dataset_generator import DatasetGenerator
from .util import collate_fn, xywh2xyminmax, classid2cocoid, cocoid2classid, iou_np, ValDataPrefetcher

import torchvision
from loguru import logger


class MultiscaleGenerator(DatasetGenerator):
    def __init__(
        self,
        exp,
        model,
        scales,
        conf,
        iou_thresh,
        rematch_thresh,
        device,
        is_distributed,
        batch_size,
        half_precision,
        oneshot_image_ids = None,
    ):
        super().__init__(
            exp = exp,
            model = model,  # manual assignment
            device = device,
            is_distributed = is_distributed,
            batch_size = batch_size,
            half_precision = half_precision,
            oneshot_image_ids = oneshot_image_ids
        )
        self.scales = scales
        self.conf_thresh = conf
        self.iou_thresh = iou_thresh
        self.rematch_thresh = rematch_thresh

    def init(self):
        from yolox.data import (
            ValTransform,
            COCODataset
        )
        import torch.distributed as dist
        from loguru import logger
        
        dataset_map = {}
        sampler_map = {}
        self.dataloader_map = {}
        for scale in self.scales:
            logger.info('Initializing scale {} ...'.format(scale))
            with wait_for_the_master(get_local_rank()):
                dataset_map[scale] = COCODataset(
                    data_dir=self.exp.data_dir,
                    json_file=self.exp.train_ann,
                    name="train2017",
                    img_size=(scale, scale),
                    preproc=ValTransform(legacy=False),
                )

            if self.is_distributed:
                target_batch_size = self.batch_size // dist.get_world_size()
                sampler_map[scale] = torch.utils.data.distributed.DistributedSampler(
                    dataset_map[scale],
                    rank=get_local_rank(),
                    num_replicas=get_world_size(), 
                    shuffle=False,
                    drop_last=False
                )
            else:
                target_batch_size = self.batch_size
                sampler_map[scale] = torch.utils.data.SequentialSampler(dataset_map[scale])

            dataloader_kwargs = {
                "num_workers": self.exp.data_num_workers,
                "pin_memory": True,
                "sampler": sampler_map[scale],
                "collate_fn": collate_fn,
            }
            dataloader_kwargs["batch_size"] = target_batch_size

            self.dataloader_map[scale] = torch.utils.data.DataLoader(dataset_map[scale], **dataloader_kwargs)

    def generate_dataset(self):
        # Scale별로 한번에 Generate한 다음 합친다.
        # 큰 스케일부터 생성한다 (Late OOM 문제 발생 가능할 수 있으므로)
        boxes_scales = {}
        clses_scales = {}
        scores_scales = {}
        image_names = set()
        for scale in reversed(sorted(self.scales)):
            all_bboxes = {}
            all_clses = {}
            all_scores = {}
            
            if self.is_distributed:
                desc_msg = "[Rank {}] Inferencing scale {}".format(get_local_rank(), scale)
            else:
                desc_msg = "Inferencing scale {}".format(scale)

            prefetcher = ValDataPrefetcher(self.dataloader_map[scale])
            pbar = tqdm(range(len(self.dataloader_map[scale])), desc=desc_msg)
            while True:
                img, target, img_info, img_id = prefetcher.next()
                if type(img) == type(None):
                    break  # End of prefetcher

                if self.half_precision:
                    img = img.to(torch.float16)

                # Infer current scale
                with torch.no_grad():
                    batched_outputs = self.model(img)
                    batched_outputs = postprocess(
                        batched_outputs, self.exp.num_classes, self.exp.test_conf,
                        self.exp.nmsthre, class_agnostic=True
                    )

                for batch_idx, output in enumerate(batched_outputs):
                    image_id = img_id[batch_idx]

                    if output is None:
                        all_bboxes[image_id] = []
                        all_clses[image_id] = []
                        all_scores[image_id] = []
                        continue

                    ratio = min(scale / img_info[0][batch_idx], scale / img_info[1][batch_idx])

                    bboxes = output[:, 0:4]
                    # preprocessing: resize
                    bboxes /= ratio
                    cls = output[:, 6]
                    scores = output[:, 4] * output[:, 5]

                    all_bboxes[image_id] = bboxes.cpu().numpy()
                    all_clses[image_id] = cls.cpu().numpy().astype(int)
                    all_scores[image_id] = scores.cpu().numpy()

                pbar.update()

            boxes_scales[scale] = all_bboxes
            clses_scales[scale] = all_clses
            scores_scales[scale] = all_scores
            image_names.update(list(all_bboxes.keys()))

        # 각 image_id에 대해 multiscale_match 수행
        results = []
        for image_id in sorted(list(image_names)):
            boxes_allscale = { scale: boxes_scales[scale][image_id] for scale in self.scales if image_id in boxes_scales[scale] }
            clses_allscale = { scale: clses_scales[scale][image_id] for scale in self.scales if image_id in clses_scales[scale] }
            scores_allscale = { scale: scores_scales[scale][image_id] for scale in self.scales if image_id in scores_scales[scale] }

            for batch_idx in range(len(boxes_allscale)):
                matched_objects, gt_nonmatched_objects, infer_nonmatched_objects = self.multiscale_match(
                    bboxes_scales = boxes_allscale,
                    cls_scales = clses_allscale,
                    scores_scales = scores_allscale,
                    image_name = image_id,
                )

                results.append([image_id, matched_objects])

        # JSON Annotation 저장하기
        images_map = { item['id']: item for item in self.annotations['images'] }
        result_annotations = []
        for image_id, bboxes in tqdm(results, desc="Organizing result bboxes"):
            for class_id in bboxes.keys():
                class_bboxes = bboxes[class_id]
                for bbox in class_bboxes:
                    bbox = [int(i) for i in bbox]  # np.int64 items does present
                    bbox = [bbox[0], bbox[1], bbox[2] - bbox[0], bbox[3] - bbox[1]]  # xyminmax2xywh
                    result_annotations.append({
                        'area': bbox[2] * bbox[3],
                        'iscrowd': 0,
                        'bbox': bbox,
                        'category_id': int(classid2cocoid(class_id)),
                        'ignore': 0,
                        'segmentation': [],
                        'image_id': image_id,
                        'id': len(result_annotations) + 1  # 1부터 시작한다.
                    })
                    
        return {
            "images": [ images_map[image_id] for image_id, bboxes in results ],
            "type": "instances",
            "annotations": result_annotations,
            "categories": self.annotations["categories"]
        }

    def multiscale_match(self, image_name, bboxes_scales, cls_scales, scores_scales):
        if image_name not in self.annotation_map:
            # 어노테이션 없는 빈 이미지 (Background)
            o_items = []
        else:
            o_items = self.annotation_map[image_name]

        o_cls = np.array(list(map(lambda item : cocoid2classid(item['category_id']), o_items))).astype(int)
        o_bboxes = np.array(list(map(lambda item : item['bbox'], o_items)))
        o_bboxes = np.array(list(map(xywh2xyminmax, o_bboxes)))

        gt_objects = {}
        for class_id in sorted(np.unique(o_cls)):
            gt_objects[class_id] = o_bboxes[np.where(o_cls == class_id)]

        infer_objects = {}
        for scale in self.scales:
            bboxes = bboxes_scales[scale]
            cls = cls_scales[scale]
            scores = scores_scales[scale]

            if np.all(bboxes == None):
                continue
            
            scoremap = np.where(scores > self.conf_thresh)
            bboxes = bboxes[scoremap]
            cls = cls[scoremap]
            scores = scores[scoremap]

            for class_id in sorted(np.unique(cls)):
                if class_id not in infer_objects:
                    infer_objects[class_id] = {}
                infer_objects[class_id][scale] = bboxes[np.where(cls == class_id)]
        
        match_table_all = {}
        gt_only_bbox_table_all = {}
        infer_only_bbox_table_all = {}

        for class_id in sorted(np.unique(np.concatenate([
            *[cls_scales[scale] for scale in self.scales if np.all(cls_scales[scale] != None)], o_cls
        ]).astype(int))):
            infer_only_extras = [] # To be used after picking infer_nonmatched

            if class_id not in infer_objects:
                match_table = {}
                gt_only_bbox_table = []
                if class_id in gt_objects:
                    for idx in range(len(gt_objects[class_id])):
                        match_table[idx] = []
                    gt_only_bbox_table = gt_objects[class_id]
                infer_only_bbox_table = []
            elif class_id not in gt_objects:
                match_table = {}
                gt_only_bbox_table = []
                infer_only_bbox_table = []
                if class_id in infer_objects:
                    for scale in sorted(infer_objects[class_id].keys()):
                        infer_only_bbox_table.extend(infer_objects[class_id][scale])
            else:
                gt_bboxes = gt_objects[class_id]
                infer_bboxes_scales = infer_objects[class_id]

                match_table = { idx: [] for idx in range(len(gt_bboxes)) }
                gt_only_bbox_table = np.copy(gt_bboxes)
                infer_only_bbox_table = []

                # First match GT with Infer
                # 매칭 결과는 match_table에 GT bbox index를 Key로 리스트로 저장
                # 매칭되지 않은 결과는 gt_only_bbox_table과 infer_only_bbox_tabLle에 저장
                for gt_idx, gt_bbox_single in enumerate(gt_bboxes):
                    over_iou_found = False

                    for scale in sorted(infer_bboxes_scales.keys()):
                        if len(infer_bboxes_scales[scale]) == 0:
                            continue

                        ious = []
                        for infer_bbox_single in infer_bboxes_scales[scale]:
                            ious.append(iou_np(gt_bbox_single, infer_bbox_single))
                        ious = np.array(ious)

                        # 비슷한 클래스에 bbox가 두개 이상 있는 경우도 존재할수 있다는것...
                        # assert np.sum(ious >= iou_thresh) in [0, 1], "More than 1 matching bbox detected!"

                        # 결국 아래에서 Max IoU인 bbox만 사용하기 때문에 무방하다.
                        maxiou_idx = np.argmax(ious)
                        if ious[maxiou_idx] >= self.iou_thresh:
                            # match_table[gt_idx].append({ scale: infer_bboxes_scales[scale] })
                            match_table[gt_idx].append(infer_bboxes_scales[scale])
                            infer_bboxes_scales[scale] = np.delete(infer_bboxes_scales[scale], maxiou_idx, axis=0)
                            over_iou_found = True
                        
                    if over_iou_found:
                        gt_newptr_idx = -1
                        for newptr_idx, gt_new_bbox_single in enumerate(gt_only_bbox_table):
                            if np.all(gt_new_bbox_single == gt_bbox_single):
                                gt_newptr_idx = newptr_idx
                                break
                        assert gt_newptr_idx != -1, "Array Inconsistency Detected!"

                        gt_only_bbox_table = np.delete(gt_only_bbox_table, gt_newptr_idx, axis=0)
                    
                # 모든 GT 매칭이 끝나고 남은 bbox를 추가한다.
                for scale in sorted(infer_bboxes_scales.keys()):
                    infer_only_bbox_table.extend(infer_bboxes_scales[scale])

            # Class-inaware per-class rematch infer_only_bbox
            # 이전 매칭 결과중 infer_only_bbox_table에 있는 박스들을 서로 매칭하여
            # 서로 N쌍 이상 매칭되는 쌍을 GT로 설정하고 해당 infer_only_bbox_table로부터 제거
            srcbbox_idx = 0
            while srcbbox_idx < len(infer_only_bbox_table):
                # Begin srcbbox loop
                srcbbox = infer_only_bbox_table[srcbbox_idx]
                
                iou_table = []
                for dstbbox in infer_only_bbox_table:  # Include self-bbox on purpose (Will be IoU=1.0)
                    iou_table.append(iou_np(srcbbox, dstbbox))
                iou_table = np.array(iou_table)

                is_srcbbox_removed = False
                iou_argsort = np.argsort(iou_table)
                if np.all(iou_table[iou_argsort][::-1][:self.rematch_thresh] >= self.iou_thresh):
                    # Remove all thresh_over_bbox from infer_only_bbox_table
                    thresh_over_flags = iou_table >= self.iou_thresh
                    thresh_over_bboxes = [bbox for idx, bbox in enumerate(infer_only_bbox_table) if thresh_over_flags[idx]]  # Reason why inclueded itself 
                    for thresh_over_bbox in thresh_over_bboxes:
                        # Find appropriate bbox and remove
                        # It will remove srcbbox as well, so no further removal required
                        for idx, dstbbox in enumerate(infer_only_bbox_table):
                            if np.all(dstbbox == thresh_over_bbox):
                                infer_only_bbox_table = np.delete(infer_only_bbox_table, idx, axis=0)
                                break
                    infer_only_extras.append(srcbbox)
                    is_srcbbox_removed = True

                if not is_srcbbox_removed:
                    srcbbox_idx += 1
                # End srcbbox loop

            match_table_target = [
                (np.array(gt_bboxes[gt_idx]).tolist(), len(match_table[gt_idx]))
                for gt_idx in match_table.keys()
                if len(match_table[gt_idx]) > 0
            ]
            match_table_target.extend([
                (np.array(item).tolist(), 1)
                for item in infer_only_extras
            ])
            if len(match_table_target) > 0:
                match_table_all[class_id] = match_table_target
            if len(gt_only_bbox_table) > 0:
                gt_only_bbox_table_all[class_id] = np.array(gt_only_bbox_table).tolist()
            if len(infer_only_bbox_table) > 0:
                infer_only_bbox_table_all[class_id] = np.array(infer_only_bbox_table).tolist()
        
        # 동일 위치에서 지배적인 클래스를 우선하는 Matching Strategy
        #
        # 전체 bbox를 돌면서 Inner match (match_table->match_table)과
        # Outer match (match_table->gt_only|infer_only)를 수행한다.
        flatten_matched_items = []
        for class_id in sorted(match_table_all.keys()):
            flatten_matched_items.extend([[*bbox, class_id, occurance] for (bbox, occurance) in match_table_all[class_id]])
        flatten_matched_items = np.array(flatten_matched_items)
            
        flatten_gt_only_items = []
        for class_id in sorted(gt_only_bbox_table_all.keys()):
            flatten_gt_only_items.extend([[*bbox, class_id, 1] for bbox in gt_only_bbox_table_all[class_id]])
        flatten_gt_only_items = np.array(flatten_gt_only_items)

        flatten_infer_only_items = []
        for class_id in sorted(infer_only_bbox_table_all.keys()):
            flatten_infer_only_items.extend([[*bbox, class_id, 1] for bbox in infer_only_bbox_table_all[class_id]])
        flatten_infer_only_items = np.array(flatten_infer_only_items)

        matched_item_idx = 0
        while matched_item_idx < len(flatten_matched_items):
            *bbox_origin, class_id, class_occurances = flatten_matched_items[matched_item_idx]
            
            iou_table = []  # Inner Match
            gt_iou_table = []  # Outer Match (GT->Matched)
            infer_iou_table = []  # Outer Match (Infer->Matched)
            
            # 지배적인 클래스를 찾아서 해당 클래스로 세팅하고,
            # flatten_items로부터 iou_over_items에 해당하는 bbox를 삭제한다.
            def sanitize_bboxes(flatten_matched_items, flatten_items, iou_over_items):
                # 지배적인 클래스 찾기
                all_classes = [class_id]
                for *_, target_class_id, target_class_occurances in iou_over_items:
                    for _ in range(int(target_class_occurances)):
                        all_classes.append(target_class_id)
                all_classes = np.array(all_classes)
                unique_class_ids, unique_class_counts = np.unique(all_classes, return_counts=True)
                dorminant_class_id = unique_class_ids[np.argmax(unique_class_counts)]

                # 겹치는 박스 모두 지우기
                for item in iou_over_items:
                    idx = -1
                    for target_idx, target_item in enumerate(flatten_items):
                        if np.all(item == target_item):
                            idx = target_idx
                            break
                    assert idx != -1
                    flatten_items = np.delete(flatten_items, idx, axis=0)

                # 현재 박스의 클래스를 지배적인 클래스로 변경하기
                flatten_matched_items[matched_item_idx] = np.array([*bbox_origin, dorminant_class_id, 1])
                return flatten_items
                
            # Inner Match
            for idx, (*bbox_target, class_id, class_occurances) in enumerate(flatten_matched_items):
                if np.all(bbox_origin == bbox_target):
                    continue
                iou_table.append((idx, iou_np(np.array(bbox_origin), np.array(bbox_target))))
            
            iou_over_items = [flatten_matched_items[idx] for idx, iou in iou_table if iou > self.iou_thresh]
            if len(iou_over_items) > 0:
                flatten_matched_items = sanitize_bboxes(flatten_matched_items, flatten_matched_items, iou_over_items)

            # Outer Match (GT->Matched)
            for idx, (*bbox_target, class_id, class_occurances) in enumerate(flatten_gt_only_items):
                gt_iou_table.append((idx, iou_np(np.array(bbox_origin), np.array(bbox_target))))
                
            iou_over_items = [flatten_gt_only_items[idx] for idx, iou in gt_iou_table if iou > self.iou_thresh]
            if len(iou_over_items) > 0:
                flatten_gt_only_items = sanitize_bboxes(flatten_matched_items, flatten_gt_only_items, iou_over_items)

            # Outer Match (Infer->Matched)
            for idx, (*bbox_target, class_id, class_occurances) in enumerate(flatten_infer_only_items):
                infer_iou_table.append((idx, iou_np(np.array(bbox_origin), np.array(bbox_target))))
                
            iou_over_items = [flatten_infer_only_items[idx] for idx, iou in infer_iou_table if iou > self.iou_thresh]
            if len(iou_over_items) > 0:
                flatten_infer_only_items = sanitize_bboxes(flatten_matched_items, flatten_infer_only_items, iou_over_items)
                
            matched_item_idx += 1

        # 다 끝난 flatten_ 박스들을 match_table 형식으로 되돌려놓는다.
        match_table_all = {}
        gt_only_bbox_table_all = {}
        infer_only_bbox_table_all = {}

        for *bbox, class_id, class_occurances in flatten_matched_items:
            class_id = int(class_id)
            if class_id not in match_table_all:
                match_table_all[class_id] = []
            match_table_all[class_id].append(bbox)
            
        for *bbox, class_id, class_occurances in flatten_gt_only_items:
            class_id = int(class_id)
            if class_id not in gt_only_bbox_table_all:
                gt_only_bbox_table_all[class_id] = []
            gt_only_bbox_table_all[class_id].append(bbox)
            
        for *bbox, class_id, class_occurances in flatten_infer_only_items:
            class_id = int(class_id)
            if class_id not in infer_only_bbox_table_all:
                infer_only_bbox_table_all[class_id] = []
            infer_only_bbox_table_all[class_id].append(bbox)

        return match_table_all, gt_only_bbox_table_all, infer_only_bbox_table_all


def postprocess_before_nms(prediction, scale, img_info, num_classes, conf_thre=0.7, class_agnostic=False):
    box_corner = prediction.new(prediction.shape)
    box_corner[:, :, 0] = prediction[:, :, 0] - prediction[:, :, 2] / 2
    box_corner[:, :, 1] = prediction[:, :, 1] - prediction[:, :, 3] / 2
    box_corner[:, :, 2] = prediction[:, :, 0] + prediction[:, :, 2] / 2
    box_corner[:, :, 3] = prediction[:, :, 1] + prediction[:, :, 3] / 2
    prediction[:, :, :4] = box_corner[:, :, :4]

    bboxes = []
    scores = []
    all_detections = []
    idxs = []
    for i, image_pred in enumerate(prediction):

        # If none are remaining => process next image
        if not image_pred.size(0):
            bboxes.append([])
            scores.append([])
            if not class_agnostic: idxs.append([])
            continue
        # Get score and class with highest confidence
        class_conf, class_pred = torch.max(image_pred[:, 5: 5 + num_classes], 1, keepdim=True)

        conf_mask = (image_pred[:, 4] * class_conf.squeeze() >= conf_thre).squeeze()
        # Detections ordered as (x1, y1, x2, y2, obj_conf, class_conf, class_pred)
        detections = torch.cat((image_pred[:, :5], class_conf, class_pred.float()), 1)
        detections = detections[conf_mask]
        if not detections.size(0):
            bboxes.append([])
            scores.append([])
            if not class_agnostic: idxs.append([])
            continue

        # postprocessing: resize
        ratio = min(scale / img_info[0][i], scale / img_info[1][i])
        detections[:, :4] /= ratio

        all_detections.append(detections)
        bboxes.append(detections[:, :4])
        scores.append(detections[:, 4] * detections[:, 5])
        if not class_agnostic:
            idxs.append(detections[:, 6])

    if class_agnostic:
        return all_detections, bboxes, scores
    else:
        return all_detections, bboxes, scores, idxs


def postprocess_after_nms(all_detections, bboxes, scores, idxs=None, nms_thre=0.45, class_agnostic=True):
    if class_agnostic:
        nms_out_index = torchvision.ops.nms(bboxes, scores, nms_thre)
    else:
        nms_out_index = torchvision.ops.batched_nms(bboxes, scores, idxs)

    detections = all_detections[nms_out_index]

    return detections


class SimpleMultiscaleGenerator(DatasetGenerator):
    def __init__(
        self,
        exp,
        model,
        scales,
        conf,
        device,
        is_distributed,
        batch_size,
        half_precision,
        oneshot_image_ids = None,
    ):
        super().__init__(
            exp = exp,
            model = model,  # manual assignment
            device = device,
            is_distributed = is_distributed,
            batch_size = batch_size,
            half_precision = half_precision,
            oneshot_image_ids = oneshot_image_ids
        )
        self.scales = scales
        self.conf_thresh = conf

    def init(self):
        from yolox.data import (
            ValTransform,
            COCODataset
        )
        import torch.distributed as dist
        from loguru import logger
        
        dataset_map = {}
        sampler_map = {}
        self.dataloader_map = {}
        for scale in self.scales:
            logger.info('Initializing scale {} ...'.format(scale))
            with wait_for_the_master(get_local_rank()):
                dataset_map[scale] = COCODataset(
                    data_dir=self.exp.data_dir,
                    json_file=self.exp.train_ann,
                    name="train2017",
                    img_size=(scale, scale),
                    preproc=ValTransform(legacy=False),
                )

            if self.is_distributed:
                target_batch_size = self.batch_size // dist.get_world_size()
                sampler_map[scale] = torch.utils.data.distributed.DistributedSampler(
                    dataset_map[scale],
                    rank=get_local_rank(),
                    num_replicas=get_world_size(), 
                    shuffle=False,
                    drop_last=False
                )
            else:
                target_batch_size = self.batch_size
                sampler_map[scale] = torch.utils.data.SequentialSampler(dataset_map[scale])

            dataloader_kwargs = {
                "num_workers": self.exp.data_num_workers,
                "pin_memory": True,
                "sampler": sampler_map[scale],
                "collate_fn": collate_fn,
            }
            dataloader_kwargs["batch_size"] = target_batch_size
            self.dataloader_map[scale] = torch.utils.data.DataLoader(dataset_map[scale], **dataloader_kwargs)

    def generate_dataset(self):
        # Scale별로 한번에 Generate한 다음 합친다.
        # 큰 스케일부터 생성한다 (Late OOM 문제 발생 가능할 수 있으므로)
        # NMS에 넣기 전에 먼저 Scale별로 Infer와 Slicing부터 수행한다
        all_detections_scales = { scale: {} for scale in self.scales }
        bboxes_scales = { scale: {} for scale in self.scales }
        scores_scales = { scale: {} for scale in self.scales }
        image_names = set()

        for scale in reversed(sorted(self.scales)):
            if self.is_distributed:
                desc_msg = "[Rank {}] Inferencing scale {}".format(get_local_rank(), scale)
            else:
                desc_msg = "Inferencing scale {}".format(scale)

            prefetcher = ValDataPrefetcher(self.dataloader_map[scale])
            pbar = tqdm(range(len(self.dataloader_map[scale])), desc=desc_msg)
            while True:
                img, target, img_info, img_id = prefetcher.next()
                if type(img) == type(None):
                    break  # End of prefetcher

                if self.half_precision:
                    img = img.to(torch.float16)

                # Infer current scale
                with torch.no_grad():
                    batched_outputs = self.model(img)
                    all_detections, bboxes, scores = postprocess_before_nms(
                        batched_outputs, scale, img_info,
                        self.exp.num_classes, self.exp.test_conf, class_agnostic=True
                    )
                    del batched_outputs

                # Convert format (np.str_ -> str)
                img_id = [str(item) for item in img_id]

                # Batched image ids
                for batch_idx, image_id in enumerate(img_id):
                    if len(bboxes[batch_idx]) != 0:
                        all_detections_scales[scale][image_id] = all_detections[batch_idx]
                        bboxes_scales[scale][image_id] = bboxes[batch_idx]
                        scores_scales[scale][image_id] = scores[batch_idx]

                # img_id는 배치 단위임
                image_names.update(img_id)
                pbar.update()

        # NMS를 수행한다
        # for scale in reversed(sorted(self.scales)):
        result_bboxes = []
        result_cls = []
        result_scores = []
        result_image_names = []
        for image_id in sorted(list(image_names)):
            all_detections = torch.cat([all_detections_scales[scale][image_id] for scale in self.scales if image_id in all_detections_scales[scale]], dim=0)
            boxes = torch.cat([bboxes_scales[scale][image_id] for scale in self.scales if image_id in bboxes_scales[scale]], dim=0)
            scores = torch.cat([scores_scales[scale][image_id] for scale in self.scales if image_id in scores_scales[scale]], dim=0)

            # Class agnostic
            output = postprocess_after_nms(
                all_detections, boxes, scores,
                nms_thre=self.exp.nmsthre, class_agnostic=True
            )

            if output is None:
                continue

            bboxes = output[:, 0:4]
            cls = output[:, 6]
            scores = output[:, 4] * output[:, 5]

            result_bboxes.append(bboxes.cpu().numpy())
            result_cls.append(cls.cpu().numpy().astype(int))
            result_scores.append(scores.cpu().numpy())
            result_image_names.append(image_id)

        # JSON Annotation 저장하기
        images_map = { item['id']: item for item in self.annotations['images'] }
        result_annotations = []
        for bboxes, cls, scores, image_id in tqdm(zip(result_bboxes, result_cls, result_scores, result_image_names), desc="Organizing result bboxes", total=len(result_bboxes)):
            for bbox, cls, score in zip(bboxes, cls, scores):
                bbox = [int(i) for i in bbox]  # np.int64 items does present
                bbox = [bbox[0], bbox[1], bbox[2] - bbox[0], bbox[3] - bbox[1]]  # xyminmax2xywh
                result_annotations.append({
                    'area': bbox[2] * bbox[3],
                    'iscrowd': 0,
                    'bbox': bbox,
                    'category_id': int(classid2cocoid(cls)),
                    'det_confidence': float(score),
                    'ignore': 0,
                    'segmentation': [],
                    'image_id': image_id,
                    'id': len(result_annotations) + 1  # 1부터 시작한다.
                })

        return {
            "images": [ images_map[image_id] for image_id in result_image_names ],
            "type": "instances",
            "annotations": result_annotations,
            "categories": self.annotations["categories"]
        }



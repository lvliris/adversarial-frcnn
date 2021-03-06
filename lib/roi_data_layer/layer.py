# --------------------------------------------------------
# Fast R-CNN with OHEM
# Licensed under The MIT License [see LICENSE for details]
# Written by Ross Girshick and Abhinav Shrivastava
# --------------------------------------------------------

"""The data layer used during training to train a Fast R-CNN network.

RoIDataLayer implements a Caffe Python layer.
"""

import caffe
from fast_rcnn.config import cfg
from roi_data_layer.minibatch import get_minibatch, get_allrois_minibatch, get_ohem_minibatch, get_ohem_minibatch_ratio
import numpy as np
import yaml
import os
import os.path 

import cv2

from multiprocessing import Process, Queue

class RoIDataLayer(caffe.Layer):
    """Fast R-CNN data layer used for training."""

    def _shuffle_roidb_inds(self):
        """Randomly permute the training roidb."""
        if cfg.TRAIN.ASPECT_GROUPING:
            widths = np.array([r['width'] for r in self._roidb])
            heights = np.array([r['height'] for r in self._roidb])
            horz = (widths >= heights)
            vert = np.logical_not(horz)
            horz_inds = np.where(horz)[0]
            vert_inds = np.where(vert)[0]
            inds = np.hstack((
                np.random.permutation(horz_inds),
                np.random.permutation(vert_inds)))
            inds = np.reshape(inds, (-1, 2))
            row_perm = np.random.permutation(np.arange(inds.shape[0]))
            inds = np.reshape(inds[row_perm, :], (-1,))
            self._perm = inds
        else:
            self._perm = np.random.permutation(np.arange(len(self._roidb)))
        self._cur = 0

    def _get_next_minibatch_inds(self):
        """Return the roidb indices for the next minibatch."""
        if self._cur + cfg.TRAIN.IMS_PER_BATCH >= len(self._roidb):
            self._shuffle_roidb_inds()

        db_inds = self._perm[self._cur:self._cur + cfg.TRAIN.IMS_PER_BATCH]
        self._cur += cfg.TRAIN.IMS_PER_BATCH
        return db_inds

    def _get_next_minibatch(self):
        """Return the blobs to be used for the next minibatch.

        If cfg.TRAIN.USE_PREFETCH is True, then blobs will be computed in a
        separate process and made available through self._blob_queue.
        """
        if cfg.TRAIN.USE_PREFETCH:
            return self._blob_queue.get()
        else:
            db_inds = self._get_next_minibatch_inds()
            minibatch_db = [self._roidb[i] for i in db_inds]
            if cfg.TRAIN.USE_OHEM:
                blobs = get_allrois_minibatch(minibatch_db, self._num_classes)
            else:
                blobs = get_minibatch(minibatch_db, self._num_classes)

            return blobs

    def set_roidb(self, roidb):
        """Set the roidb to be used by this layer during training."""
        self._roidb = roidb
        self._shuffle_roidb_inds()
        if cfg.TRAIN.USE_PREFETCH:
            self._blob_queue = Queue(10)
            self._prefetch_process = BlobFetcher(self._blob_queue,
                                                 self._roidb,
                                                 self._num_classes)
            self._prefetch_process.start()
            # Terminate the child process when the parent exists
            def cleanup():
                print 'Terminating BlobFetcher'
                self._prefetch_process.terminate()
                self._prefetch_process.join()
            import atexit
            atexit.register(cleanup)

    def setup(self, bottom, top):
        """Setup the RoIDataLayer."""

        # parse the layer parameter string, which must be valid YAML
        layer_params = yaml.load(self.param_str_)

        self._num_classes = int(layer_params['num_classes'])

        self._name_to_top_map = {}

        # data blob: holds a batch of N images, each with 3 channels
        idx = 0
        top[idx].reshape(cfg.TRAIN.IMS_PER_BATCH, 3,
            max(cfg.TRAIN.SCALES), cfg.TRAIN.MAX_SIZE)
        self._name_to_top_map['data'] = idx
        idx += 1

        if cfg.TRAIN.HAS_RPN:
            top[idx].reshape(1, 3)
            self._name_to_top_map['im_info'] = idx
            idx += 1

            top[idx].reshape(1, 4)
            self._name_to_top_map['gt_boxes'] = idx
            idx += 1
        else: # not using RPN
            # rois blob: holds R regions of interest, each is a 5-tuple
            # (n, x1, y1, x2, y2) specifying an image batch index n and a
            # rectangle (x1, y1, x2, y2)
            top[idx].reshape(1, 5)
            self._name_to_top_map['rois'] = idx
            idx += 1

            # labels blob: R categorical labels in [0, ..., K] for K foreground
            # classes plus background
            top[idx].reshape(1)
            self._name_to_top_map['labels'] = idx
            idx += 1

            if cfg.TRAIN.BBOX_REG:
                # bbox_targets blob: R bounding-box regression targets with 4
                # targets per class
                top[idx].reshape(1, self._num_classes * 4)
                self._name_to_top_map['bbox_targets'] = idx
                idx += 1

                # bbox_inside_weights blob: At most 4 targets per roi are active;
                # thisbinary vector sepcifies the subset of active targets
                top[idx].reshape(1, self._num_classes * 4)
                self._name_to_top_map['bbox_inside_weights'] = idx
                idx += 1

                top[idx].reshape(1, self._num_classes * 4)
                self._name_to_top_map['bbox_outside_weights'] = idx
                idx += 1

        print 'RoiDataLayer: name_to_top:', self._name_to_top_map
        assert len(top) == len(self._name_to_top_map)

    def forward(self, bottom, top):
        """Get blobs and copy them into this layer's top blob vector."""
        blobs = self._get_next_minibatch()

        for blob_name, blob in blobs.iteritems():
            top_ind = self._name_to_top_map[blob_name]
            # Reshape net's input blobs
            top[top_ind].reshape(*(blob.shape))
            # Copy data into net's input blobs
            top[top_ind].data[...] = blob.astype(np.float32, copy=False)

    def backward(self, top, propagate_down, bottom):
        """This layer does not propagate gradients."""
        pass

    def reshape(self, bottom, top):
        """Reshaping happens during the call to forward."""
        pass

class OHEMDataLayer(caffe.Layer):
    """Online Hard-example Mining Layer."""
    def setup(self, bottom, top):
        """Setup the OHEMDataLayer."""

        # parse the layer parameter string, which must be valid YAML
        layer_params = yaml.load(self.param_str_)

        self._num_classes = layer_params['num_classes']
        self._iter_size = layer_params['iter_size']
        self._maintain_before = layer_params['maintain_before']
        self._count_iter = 0 

        self._name_to_bottom_map = {
            'cls_prob_readonly': 0,
            'bbox_pred_readonly': 1,
            'rois': 2,
            'labels': 3}

        if cfg.TRAIN.BBOX_REG:
            self._name_to_bottom_map['bbox_targets'] = 4
            self._name_to_bottom_map['bbox_loss_weights'] = 5

        self._name_to_top_map = {}

        assert cfg.TRAIN.HAS_RPN == False
        # data blob: holds a batch of N images, each with 3 channels
        idx = 0
        # rois blob: holds R regions of interest, each is a 5-tuple
        # (n, x1, y1, x2, y2) specifying an image batch index n and a
        # rectangle (x1, y1, x2, y2)
        top[idx].reshape(1, 5)
        self._name_to_top_map['rois_hard'] = idx
        idx += 1

        # labels blob: R categorical labels in [0, ..., K] for K foreground
        # classes plus background
        top[idx].reshape(1)
        self._name_to_top_map['labels_hard'] = idx
        idx += 1

        if cfg.TRAIN.BBOX_REG:
            # bbox_targets blob: R bounding-box regression targets with 4
            # targets per class
            top[idx].reshape(1, self._num_classes * 4)
            self._name_to_top_map['bbox_targets_hard'] = idx
            idx += 1

            # bbox_inside_weights blob: At most 4 targets per roi are active;
            # thisbinary vector sepcifies the subset of active targets
            top[idx].reshape(1, self._num_classes * 4)
            self._name_to_top_map['bbox_inside_weights_hard'] = idx
            idx += 1

            top[idx].reshape(1, self._num_classes * 4)
            self._name_to_top_map['bbox_outside_weights_hard'] = idx
            idx += 1

        # used for ASDN
        if cfg.TRAIN.USE_ASDN: 
            top[idx].reshape(*(bottom[0].data.shape))
            self._name_to_top_map['prop_before'] = idx
            idx += 1


        print 'OHEMDataLayer: name_to_top:', self._name_to_top_map
        assert len(top) == len(self._name_to_top_map)

    def forward(self, bottom, top):
        """Compute loss, select RoIs using OHEM. Use RoIs to get blobs and copy them into this layer's top blob vector."""

        cls_prob = bottom[0].data
        bbox_pred = bottom[1].data
        rois = bottom[2].data
        labels = bottom[3].data

        self._count_iter = (self._count_iter + 1) % self._iter_size

        if cfg.TRAIN.BBOX_REG:
            bbox_target = bottom[4].data
            bbox_inside_weights = bottom[5].data
            bbox_outside_weights = bottom[6].data
        else:
            bbox_target = None
            bbox_inside_weights = None
            bbox_outside_weights = None

        flt_min = np.finfo(float).eps
        # classification loss
        loss = [ -1 * np.log(max(x, flt_min)) \
            for x in [cls_prob[i,label] for i, label in enumerate(labels)]]

        if cfg.TRAIN.BBOX_REG:
            # bounding-box regression loss
            # d := w * (b0 - b1)
            # smoothL1(x) = 0.5 * x^2    if |x| < 1
            #               |x| - 0.5    otherwise
            def smoothL1(x):
                if abs(x) < 1:
                    return 0.5 * x * x
                else:
                    return abs(x) - 0.5

            bbox_loss = np.zeros(labels.shape[0])
            for i in np.where(labels > 0 )[0]:
                indices = np.where(bbox_inside_weights[i,:] != 0)[0]
                bbox_loss[i] = sum(bbox_outside_weights[i,indices] * [smoothL1(x) \
                    for x in bbox_inside_weights[i,indices] * (bbox_pred[i,indices] - bbox_target[i,indices])])
            loss += bbox_loss

        blobs = []
        hard_inds = []


        if self._count_iter < self._maintain_before  or cfg.TRAIN.OHEM_RATIO < 0.1: 
            blobs, hard_inds = get_ohem_minibatch(loss, rois, labels, bbox_target, bbox_inside_weights, bbox_outside_weights)
        else:
            blobs, hard_inds = get_ohem_minibatch_ratio(loss, rois, labels, bbox_target, bbox_inside_weights, bbox_outside_weights, ratio=cfg.TRAIN.OHEM_RATIO, hard_negative=cfg.TRAIN.OHEM_HARD_NEG)
        
        for blob_name, blob in blobs.iteritems():
            top_ind = self._name_to_top_map[blob_name]
            # Reshape net's input blobs
            top[top_ind].reshape(*(blob.shape))
            # Copy data into net's input blobs
            top[top_ind].data[...] = blob.astype(np.float32, copy=False)

        # used for ASDN
        if cfg.TRAIN.USE_ASDN:
            prop_before = cls_prob[hard_inds]
            top_ind = self._name_to_top_map['prop_before']
            top[top_ind].reshape(*(prop_before.shape))
            top[top_ind].data[...] = prop_before.astype(np.float32, copy=False)



    def backward(self, top, propagate_down, bottom):
        """This layer does not propagate gradients."""
        pass

    def reshape(self, bottom, top):
        """Reshaping happens during the call to forward."""
        pass


class ASDNPretrainLossLayer(caffe.Layer):
    def setup(self, bottom, top):
        """Setup the ASDNPretrainLossLayer."""

        # parse the layer parameter string, which must be valid YAML
        layer_params = yaml.load(self.param_str_)
        self._num_classes = layer_params['num_classes']
        self._drop_neg = layer_params['drop_neg']

        self.ignore_label = None

        # mask_pred 1 means block, 0 means maintain 

        self._count = 0 

        self._name_to_bottom_map = {
            'mask_pred': 0,
            'conv_feat_mask': 1,
            'prop': 2,
            'labels_pos': 3, 
            'rois_pos': 4, 
            'data': 5}

        # (n, x1, y1, x2, y2) specifying an image batch index n and a
        # rectangle (x1, y1, x2, y2)

        # mask_thres 0 means block, 1 means maintain 

        self._name_to_top_map = {
            'loss': 0}

        top[0].reshape(1)

        print 'ASDNPretrainLossLayer: name_to_top:', self._name_to_top_map
        assert len(top) == len(self._name_to_top_map)

    def forward(self, bottom, top):
        mask_pred = bottom[0].data
        conv_feat_mask = bottom[1].data
        prop =  bottom[2].data
        labels_pos = bottom[3].data
        rois_pos = bottom[4].data
        data = bottom[5].data 

        N = bottom[0].shape[0]      # 1 * num_pos
        N2 = bottom[1].shape[0]     # 16 * num_pos

        assert(bottom[3].shape[0] == N)
        assert(bottom[2].shape[0] == N2)
        assert(bottom[4].shape[0] == N)

        prop = np.reshape(prop, (N2, self._num_classes))

        attempts = N2 / N
        pool_len = bottom[0].shape[2]

        count_bit = 1 
        for i in range(len(bottom[0].shape)):
            count_bit = count_bit * bottom[0].shape[i]

        mask_label = np.zeros((N, 1, pool_len, pool_len))

        for i in range(N):
            min_prop = 1e6
            min_id   = 0 
            nowlbl = int(labels_pos[i])
            assert(nowlbl > 0)
            for j in range(attempts):
                if min_prop > prop[j * N + i][nowlbl]: 
                    min_prop = prop[j * N + i][nowlbl]
                    min_id = j * N + i

            mask_label[i, :, :, :] = conv_feat_mask[min_id]

        # copy from: https://github.com/philkr/voc-classification/blob/master/src/python_layers.py#L52
        f, df, t = bottom[0].data, bottom[0].diff, mask_label
        mask = (self.ignore_label is None or t != self.ignore_label)
        lZ  = np.log(1+np.exp(-np.abs(f))) * mask
        dlZ = np.exp(np.minimum(f,0))/(np.exp(np.minimum(f,0))+np.exp(-np.maximum(f,0))) * mask


        top[0].data[...] = np.sum(lZ + ((f>0)-t)*f * mask) / count_bit
        df[...] = (dlZ - t*mask) / count_bit

        bp_mask = np.ones(mask_label.shape)

        if self._drop_neg == True:

            rand_thres = 0.4
            randnum = np.random.random(df.shape) 
            rand_mask = np.where(randnum < rand_thres)
            bp_mask = np.copy(mask_label)
            bp_mask[rand_mask] = 1

            df = df * bp_mask




        ###### debug 
        debug_sign = True
        debug_folder = '/scratch/xiaolonw/frcnn_debug_drop/'


        if debug_sign and os.path.exists(debug_folder):
            print_num = 10 
            print_rp = np.random.permutation(np.arange(N))

            self._count = self._count + 1

            if print_num > N: 
                print_num = N 

            for i in range(print_num):
                print_id = print_rp[i]
                print_label = mask_label[print_id]
                print_pred  = mask_pred[print_id]
                print_bp    = bp_mask[print_id]

                print_pred  = 1 / (1 + np.exp(- print_pred))

                print_label = print_label * 255
                print_label = print_label.astype(np.uint8)
                filename = debug_folder + str(self._count) + '_' + str(i) + '_gt.jpg'
                print_label = np.reshape(print_label, (7,7,1))
                print_label = 255 - print_label
                cv2.imwrite(filename, print_label)

                print_pred = print_pred * 255
                print_pred = print_pred.astype(np.uint8)
                filename = debug_folder + str(self._count) + '_' + str(i) + '_pred.jpg' 
                print_pred = np.reshape(print_pred, (7,7,1))
                print_pred = 255 - print_pred
                cv2.imwrite(filename, print_pred)

                print_bp = print_bp * 255
                print_bp = print_bp.astype(np.uint8)
                filename = debug_folder + str(self._count) + '_' + str(i) + '_bpmask.jpg' 
                print_bp = np.reshape(print_bp, (7,7,1))
                cv2.imwrite(filename, print_bp)


                # rgb 

                roi = rois_pos[print_id]
                imid = roi[0]
                x1 = roi[1]
                y1 = roi[2]
                x2 = roi[3]
                y2 = roi[4]

                im = data[imid]
                im = im[:, y1:y2, x1:x2]
                im = im.transpose((1, 2, 0)).copy()
                im += cfg.PIXEL_MEANS
                # im = im[:, :, (2, 1, 0)]

                im = cv2.resize(im, (100,100))

                im = im.astype(np.uint8)
                filename = debug_folder + str(self._count) + '_' + str(i) + '_rgb.jpg' 
                cv2.imwrite(filename, im)





    def backward(self, top, propagate_down, bottom):
        bottom[0].diff[...] *= top[0].diff


    def reshape(self, bottom, top):
        """Reshaping happens during the call to forward."""
        pass





class ASDNPretrainLabelLayer(caffe.Layer):
    def setup(self, bottom, top):
        """Setup the ASDNDataLayer."""

        # parse the layer parameter string, which must be valid YAML
        layer_params = yaml.load(self.param_str_)

        # mask_pred 1 means block, 0 means maintain 

        self._name_to_bottom_map = {
            'conv_feat': 0,
            'labels': 1,
            'rois': 2}

        # mask_thres 0 means block, 1 means maintain 

        self._name_to_top_map = {
            'conv_feat_pos': 0,
            'labels_pos': 1,
            'rois_pos': 2}

        top[0].reshape(*(bottom[0].data.shape))
        top[1].reshape(1)
        top[2].reshape(*(bottom[2].data.shape))


        print 'ASDNPretrainLabelLayer: name_to_top:', self._name_to_top_map
        assert len(top) == len(self._name_to_top_map)


    def forward(self, bottom, top):


        conv_feat = np.copy(bottom[0].data)
        labels = np.copy(bottom[1].data)
        rois = np.copy(bottom[2].data)

        sample_num = conv_feat.shape[0]
        channels   = conv_feat.shape[1]
        pool_len   = conv_feat.shape[2]

        labels = np.reshape(labels, sample_num)

        count_pos = 0

        for i in range(sample_num):
            if labels[i] > 0: 
                count_pos = count_pos + 1

        conv_feat_pos = np.zeros((count_pos, channels, pool_len, pool_len))
        labels_pos    = np.zeros(count_pos)
        rois_pos = np.zeros((count_pos, rois.shape[1]))

        cnt = 0

        for i in range(sample_num):
            if labels[i] > 0:
                labels_pos[cnt] = labels[i]
                conv_feat_pos[cnt] = np.copy(conv_feat[i])
                rois_pos[cnt, :] = np.copy(rois[i, :])
                cnt = cnt + 1

        top[0].reshape(*(conv_feat_pos.shape))
        top[0].data[...] = conv_feat_pos.astype(np.float32, copy=False)

        top[1].reshape(*(labels_pos.shape))
        top[1].data[...] = labels_pos.astype(np.float32, copy=False)

        top[2].reshape(*(rois_pos.shape))
        top[2].data[...] = rois_pos.astype(np.float32, copy=False)


        

    def backward(self, top, propagate_down, bottom):
        """This layer does not propagate gradients."""
        pass

    def reshape(self, bottom, top):
        """Reshaping happens during the call to forward."""
        pass




class ASDNPretrainDataLayer(caffe.Layer):
    def setup(self, bottom, top):
        """Setup the ASDNDataLayer."""

        # parse the layer parameter string, which must be valid YAML
        layer_params = yaml.load(self.param_str_)

        self._drop_size  = layer_params['drop_size']
        self._drop_stride  = layer_params['drop_stride']


        # mask_pred 1 means block, 0 means maintain 

        self._name_to_bottom_map = {
            'conv_feat': 0}

        # mask_thres 0 means block, 1 means maintain 

        self._name_to_top_map = {
            'conv_feat_rep': 0,
            'conv_feat_mask': 1}

        top[0].reshape(*(bottom[0].data.shape))
        top[1].reshape(*(bottom[0].data.shape))

        print 'ASDNPretrainDataLayer: name_to_top:', self._name_to_top_map
        assert len(top) == len(self._name_to_top_map)


    def generate_feature(self, conv_feat):

        sample_num = conv_feat.shape[0]
        channels   = conv_feat.shape[1]
        pool_len   = conv_feat.shape[2]

        drop_size  = self._drop_size
        drop_stride = self._drop_stride

        rep_num = int(np.ceil(float(pool_len) / float(drop_stride)))
        rep_num_area = rep_num * rep_num

        conv_feat_rep = np.zeros((sample_num * rep_num_area, channels, pool_len, pool_len))
        conv_feat_mask = np.zeros((sample_num * rep_num_area, 1, pool_len, pool_len))

        cnt = 0 

        for i in range(rep_num):
            for j in range(rep_num):

                now_feat = np.copy(conv_feat)

                startx = i * drop_stride
                starty = j * drop_stride

                if startx + drop_size > pool_len: 
                    startx = startx - 1
                if starty + drop_size > pool_len:
                    starty = starty - 1

                endx   = np.min( (startx + drop_size, pool_len) )
                endy   = np.min( (starty + drop_size, pool_len) )

                now_feat[:,:, startx : endx, starty : endy ] = now_feat[:,:, startx : endx, starty : endy ] * 0.0
                conv_feat_rep[ cnt * sample_num : cnt * sample_num + sample_num, :, :, : ] = np.copy(now_feat)

                conv_feat_mask[ cnt * sample_num : cnt * sample_num + sample_num, :, startx : endx, starty : endy] = 1

                cnt = cnt + 1

        return conv_feat_rep, conv_feat_mask


    def forward(self, bottom, top):


        conv_feat = np.copy(bottom[0].data)

        conv_feat_rep, conv_feat_mask = self.generate_feature(conv_feat)

	print 'top[0].data.shape', top[0].data.shape
        top[0].reshape(*(conv_feat_rep.shape))
	print 'top[0].data.shape', top[0].data.shape
        top[0].data[...] = conv_feat_rep.astype(np.float32, copy=False)

        top[1].reshape(*(conv_feat_mask.shape))
        top[1].data[...] = conv_feat_mask.astype(np.float32, copy=False)
        

    def backward(self, top, propagate_down, bottom):
        """This layer does not propagate gradients."""
        pass

    def reshape(self, bottom, top):
        """Reshaping happens during the call to forward."""
        pass




class ASDNLossLayer(caffe.Layer):
    def setup(self, bottom, top):
        """Setup the ASDNLossLayer."""

        # parse the layer parameter string, which must be valid YAML
        layer_params = yaml.load(self.param_str_)

        self._num_classes = layer_params['num_classes']
        self._score_thres = layer_params['score_thres']

        self._iter_size = layer_params['iter_size']
        self._maintain_before = layer_params['maintain_before'] # maintain the first image unchanged 
        self._count_iter = 0 


        self.ignore_label = None
        self._count = 0


        # mask_pred 1 means block, 0 means maintain 

        self._name_to_bottom_map = {
            'mask_pred': 0,
            'mask_thres': 1,
            'prop_before': 2,
            'prop_after': 3, 
            'labels': 4, 
            'rois': 5,
            'data': 6}

        # mask_thres 0 means block, 1 means maintain 

        self._name_to_top_map = {
            'loss': 0}

        top[0].reshape(1)

        print 'ASDNLossLayer: name_to_top:', self._name_to_top_map
        assert len(top) == len(self._name_to_top_map)

    def forward(self, bottom, top):
        
        N = bottom[0].shape[0]

        self._count_iter = (self._count_iter + 1) % self._iter_size

        if self._count_iter < self._maintain_before:
            # top[0].data[0] = 0.0
            f, df, t = bottom[0].data, bottom[0].diff, bottom[1].data
            df[...] = np.zeros(f.shape)
            return 


        count_bit = 1 
        for i in range(len(bottom[0].shape)):
            count_bit = count_bit * bottom[0].shape[i]

        mask_pred  = bottom[0].data
        mask_label = bottom[1].data

        prop_before = bottom[2].data
        prop_after  = bottom[3].data
        labels      = bottom[4].data
        rois = bottom[5].data
        data = bottom[6].data 

        prop_before = np.reshape(prop_before, (N, self._num_classes))
        prop_after = np.reshape(prop_after, (N, self._num_classes))
        labels = np.reshape(labels, N)


        # copy from: https://github.com/philkr/voc-classification/blob/master/src/python_layers.py#L52
        f, df, t = bottom[0].data, bottom[0].diff, bottom[1].data
        mask = (self.ignore_label is None or t != self.ignore_label)
        lZ  = np.log(1+np.exp(-np.abs(f))) * mask
        dlZ = np.exp(np.minimum(f,0))/(np.exp(np.minimum(f,0))+np.exp(-np.maximum(f,0))) * mask


        # top[0].data[...] = np.sum(lZ + ((f>0)-t)*f * mask) / N
        # df[...] = (dlZ - t*mask) / N

        lZ = lZ + ((f>0)-t)*f * mask
        df[...] = (dlZ - t*mask) / count_bit

        for i in range(N):
            lbl = int(labels[i])
            prop_before_select = prop_before[i][lbl]
            prop_after_select = prop_after[i][lbl]

            if (lbl > 0 and prop_after_select + self._score_thres < prop_before_select) == False :
                lZ[i] = lZ[i] * 0.0
                df[i] = lZ[i] * 0.0

        top[0].data[...] = np.sum(lZ) / count_bit





        ###### debug 
        debug_sign = True
        debug_folder = '/scratch/xiaolonw/frcnn_debug_ft/'


        if debug_sign and os.path.exists(debug_folder):

            self._count = self._count + 1
            cnt = 0 
            for i in range(N):
                print_id = i
                lbl = labels[i]
                prop_before_select = prop_before[i][lbl]
                prop_after_select = prop_after[i][lbl]

                if (lbl > 0 and prop_after_select + self._score_thres < prop_before_select) == False :
                    continue

                cnt = cnt + 1
                if cnt > 10:
                    break

                print_label = mask_label[print_id]
                print_pred  = mask_pred[print_id]

                print_pred  = 1 / (1 + np.exp(- print_pred))

                print_label = print_label * 255
                print_label = print_label.astype(np.uint8)
                filename = debug_folder + str(self._count) + '_' + str(cnt) + '_gt.jpg'
                print_label = np.reshape(print_label, (7,7,1))
                print_label = 255 - print_label
                cv2.imwrite(filename, print_label)

                print_pred = print_pred * 255
                print_pred = print_pred.astype(np.uint8)
                filename = debug_folder + str(self._count) + '_' + str(cnt) + '_pred.jpg' 
                print_pred = np.reshape(print_pred, (7,7,1))
                print_pred = 255 - print_pred
                cv2.imwrite(filename, print_pred)

                # rgb 

                roi = rois[print_id]
                imid = roi[0]
                x1 = roi[1]
                y1 = roi[2]
                x2 = roi[3]
                y2 = roi[4]

                im = data[imid]
                im = im[:, y1:y2, x1:x2]
                im = im.transpose((1, 2, 0)).copy()
                im += cfg.PIXEL_MEANS
                # im = im[:, :, (2, 1, 0)]

                im = cv2.resize(im, (100,100))

                im = im.astype(np.uint8)
                filename = debug_folder + str(self._count) + '_' + str(cnt) + '_rgb.jpg' 
                cv2.imwrite(filename, im)




    
    def backward(self, top, prop, bottom):
        bottom[0].diff[...] *= top[0].diff


    def reshape(self, bottom, top):
        """Reshaping happens during the call to forward."""
        pass







class ASDNDataLayer(caffe.Layer):
    def setup(self, bottom, top):
        """Setup the ASDNDataLayer."""

        # parse the layer parameter string, which must be valid YAML
        layer_params = yaml.load(self.param_str_)

        self._count_drop  = layer_params['count_drop']
        self._permute_count  = layer_params['permute_count']
        self._count_drop_neg = layer_params['count_drop_neg']
        self._channels = layer_params['channels']

        self._iter_size = layer_params['iter_size']
        self._maintain_before = layer_params['maintain_before'] # maintain the first image unchanged 

        self._count_iter = 0


        # mask_pred 1 means block, 0 means maintain 

        self._name_to_bottom_map = {
            'mask_pred': 0,
            'labels': 1}

        # mask_thres 0 means block, 1 means maintain 

        self._name_to_top_map = {
            'mask_thres': 0,
            'mask_thres_block': 1}


        top[0].reshape(*(bottom[0].data.shape))
        # top[1].reshape(*(bottom[0].data.shape))
        top[1].reshape(bottom[0].data.shape[0], self._channels, bottom[0].data.shape[2], bottom[0].data.shape[3])


        print 'ASDNDataLayer: name_to_top:', self._name_to_top_map
        assert len(top) == len(self._name_to_top_map)


    def generate_mask_rand(self, mask_pred):
        
        pool_len = mask_pred.shape[2]
        sample_num = mask_pred.shape[0]

        rand_mask = np.ones((sample_num, 1, pool_len, pool_len))
        mask_pixels = pool_len * pool_len
        count_drop_neg = self._count_drop_neg

        for i in range(sample_num):
            rp = np.random.permutation(np.arange(mask_pixels))
            rp = rp[0: count_drop_neg]

            now_mask = np.ones(mask_pixels)
            now_mask[rp] = 0 

            now_mask = np.reshape(now_mask, (pool_len, pool_len))
            rand_mask[i,0,:,:] = np.copy(now_mask)

        return rand_mask

    def thres_mask_rand(self, mask_pred, labels):

        pool_len = mask_pred.shape[2]
        sample_num = mask_pred.shape[0]
        labels = np.reshape(labels, sample_num)

        mask_pixels = pool_len * pool_len
        mask_pred   = 1 - mask_pred

        count_drop = self._count_drop
        permute_count = self._permute_count
        count_drop_neg = self._count_drop_neg

        mask_thres = np.ones((sample_num, 1, pool_len, pool_len))

        for i in range(sample_num):

            if labels[i] == 0:


                now_mask = np.ones(mask_pixels)

                if count_drop_neg > 0:
                    rp = np.random.permutation(np.arange(mask_pixels))
                    rp = rp[0: count_drop_neg]
                    now_mask[rp] = 0 

                now_mask = np.reshape(now_mask, (pool_len, pool_len))
                mask_thres[i,0,:,:] = np.copy(now_mask)

            else:

                rp = np.random.permutation(np.arange(permute_count))
                rp = rp[0: count_drop]

                now_mask_pred = mask_pred[i]
                now_mask_pred_array = np.reshape(now_mask_pred, mask_pixels)

                sorted_ids = np.argsort(now_mask_pred_array) 
                now_ids = sorted_ids[rp]

                now_mask = np.ones(mask_pixels)
                now_mask[now_ids] = 0 
                now_mask = np.reshape(now_mask, (pool_len, pool_len))

                mask_thres[i,0,:,:] = np.copy(now_mask)

        return mask_thres



    def forward(self, bottom, top):


        mask_pred = np.copy(bottom[0].data)
        labels = np.copy(bottom[1].data)


        self._count_iter = (self._count_iter + 1) % self._iter_size

        mask_thres = np.ones(mask_pred.shape)

        if self._count_iter >= self._maintain_before:
            mask_thres = self.thres_mask_rand(mask_pred, labels)
        
        mask_thres_block = np.tile(mask_thres, [1, self._channels, 1, 1])

        mask_thres = 1 - mask_thres # for mask labels 


        top_ind = self._name_to_top_map['mask_thres']
        top[top_ind].reshape(*(mask_thres.shape))
        top[top_ind].data[...] = mask_thres.astype(np.float32, copy=False)

        top_ind = self._name_to_top_map['mask_thres_block']
        top[top_ind].reshape(*(mask_thres_block.shape))
        top[top_ind].data[...] = mask_thres_block.astype(np.float32, copy=False)

        

    def backward(self, top, propagate_down, bottom):
        """This layer does not propagate gradients."""
        pass


class ASTNDataLayer(caffe.Layer):
    def setup(self, bottom, top):
        """Setup the ASTNDataLayer"""

        # parse the layer parameter string, which must be valid YAML
        layer_params = yaml.load(self.param_str_)

        self._block_num = layer_params['block_num']
        self._name_to_bottom_map = {
            'trans_param': 0,
            'pool5': 1
        }

        self._name_to_top_map = {
            'trans_feat': 0
        }

        top[0].reshape(*(bottom[1].data.shape))
	# print "top[0].shape: ", top[0].data.shape

        print 'ASTNDataLayer: name_to_top:', self._name_to_top_map


    def generate_grid(self, target, channels, alpha):
        """Rotate some channels by alpha"""

        sina = np.sin(alpha)
        cosa = np.cos(alpha)

        trans_param = np.array([[cosa, sina],
                                [-sina, cosa]])

        batch_size = target.shape[0]
        height = target.shape[2]
        width = target.shape[3]

        # create normalized 2D grid
        x = np.linspace(-1.0, 1.0, width)
        y = np.linspace(-1.0, 1.0, height)
        x_t, y_t = np.meshgrid(x, y)

        # flatten
        x_t_flat = np.reshape(x_t, [-1])
        y_t_flat = np.reshape(y_t, [-1])
        trans_mat = np.stack((x_t_flat, y_t_flat), axis=0)

        # generate the grid
        xy_s = np.matmul(trans_param, trans_mat)
        batch_grid = np.tile(xy_s, (channels, 1, 1))

        return batch_grid


    def bilinear_sample(self, source, target, batch_grid):
        batch_size = source.shape[0]
        channels = source.shape[1]
        height = source.shape[2]
        width = source.shape[3]

        x_max = np.array([width -1])
        y_max = np.array([height -1])
        xy_max = np.stack((x_max, y_max))

        # rescale to [0, w-1/h-1]
        scaled_grid = 0.5 * (batch_grid + 1.0) * xy_max
        x = scaled_grid[:,:,0,:]
        y = scaled_grid[:,:,1,:]
	# print x, y

        # grab 4 nearest corner points
        x0 = np.floor(x).astype(np.int32)
        x1 = np.floor(x + 1).astype(np.int32)
        y0 = np.floor(y).astype(np.int32)
        y1 = np.floor(y + 1).astype(np.int32)
	# print 'x0.shape', x0.shape

        # clip to range [0,h-1/w-1] to not violate image boudaries
        x0 = np.clip(x0, 0, x_max)
        x1 = np.clip(x1, 0, x_max)
        y0 = np.clip(y0, 0, y_max)
        y1 = np.clip(y1, 0, y_max)
	# print 'x0.shape', x0.shape

        # get the image pixle at corners
	Ia = np.zeros([batch_size, channels, height*width])
	Ib = np.zeros([batch_size, channels, height*width])
	Ic = np.zeros([batch_size, channels, height*width])
	Id = np.zeros([batch_size, channels, height*width])
	for i in range(batch_size):
		for j in range(channels):
        		Ia[i, j, ...] = source[i, j, x0[i, j, :],y0[i, j, :]]
        		Ib[i, j, ...] = source[i, j, x0[i, j, :],y1[i, j, :]]
        		Ic[i, j, ...] = source[i, j, x1[i, j, :],y0[i, j, :]]
        		Id[i, j, ...] = source[i, j, x1[i, j, :],y1[i, j, :]]
	# print 'source.shape', source.shape
	# print 'Ia.shape', Ia.shape

        # recast to float for delta calculation
        x0 = x0.astype(np.float32)
        x1 = x1.astype(np.float32)
        y0 = y0.astype(np.float32)
        y1 = y1.astype(np.float32)

        # calculate delta
        wa = (x1 - x) * (y1 - y)
        wb = (x1 - x) * (y - y0)
        wc = (x - x0) * (y1 - y)
        wd = (x - x0) * (y - y0)
	# print 'wa.shape', wa.shape

        # add dimention for addition
        # wa = np.expand_dims(wa, axis=3)
        # wb = np.expand_dims(wb, axis=3)
        # wc = np.expand_dims(wc, axis=3)
        # wd = np.expand_dims(wd, axis=3)
	# print wa.shape

	temp = Ia*wa + Ib*wb + Ic*wc + Id*wd
	# print 'temp.shape', temp.shape
        target[...] = temp.reshape(source.shape)
	# np.matmul(Ia, wa) + np.matmul(Ib, wb) + np.matmul(Ic, wc) + np.matmul(Id, wd)


    def calcu_diff(self, feature, batch_grid):
        batch_size = feature.shape[0]
        channels = feature.shape[1]
        height = feature.shape[2]
        width = feature.shape[3]

        x_max = np.array([width - 1])
        y_max = np.array([height - 1])
        xy_max = np.stack((x_max, y_max))

        # rescale to [0, w-1/h-1]
        scaled_grid = 0.5 * (batch_grid + 1.0) * xy_max
        x = scaled_grid[:, :, 0, :]
        y = scaled_grid[:, :, 1, :]

        # grab 4 nearest corner points
        x0 = np.floor(x).astype(np.int32)
        x1 = np.floor(x + 1).astype(np.int32)
        y0 = np.floor(y).astype(np.int32)
        y1 = np.floor(y + 1).astype(np.int32)

        # clip to range [0,h-1/w-1] to not violate image boudaries
        x0 = np.clip(x0, 0, x_max)
        x1 = np.clip(x1, 0, x_max)
        y0 = np.clip(y0, 0, y_max)
        y1 = np.clip(y1, 0, y_max)

        # get the image pixle at corners
	Ia = np.zeros([batch_size, channels, height*width])
	Ib = np.zeros([batch_size, channels, height*width])
	Ic = np.zeros([batch_size, channels, height*width])
	Id = np.zeros([batch_size, channels, height*width])
	for i in range(batch_size):
		for j in range(channels):
        		Ia[i, j, ...] = feature[i, j, x0[i, j, :],y0[i, j, :]]
        		Ib[i, j, ...] = feature[i, j, x0[i, j, :],y1[i, j, :]]
        		Ic[i, j, ...] = feature[i, j, x1[i, j, :],y0[i, j, :]]
        		Id[i, j, ...] = feature[i, j, x1[i, j, :],y1[i, j, :]]

        # recast to float for delta calculation
        x0 = x0.astype(np.float32)
        x1 = x1.astype(np.float32)
        y0 = y0.astype(np.float32)
        y1 = y1.astype(np.float32)

        dx = Ia * (y - y1) + Ib * (y0 - y) + Ic * (y1 - y) + Id * (y - y0)
        dy = Ia * (x - x1) + Ib * (x1 - x) + Ic * (x0 - x) + Id * (x - x0)

        return dx, dy


    def forward(self, bottom, top):
        trans_param = np.copy(bottom[0].data)
        rois_feat = np.copy(bottom[1].data)
        # print 'trans_param shape', trans_param.shape
        # print 'rois_feat shape', rois_feat.shape


        batch_size = rois_feat.shape[0]
        channel_num = rois_feat.shape[1]
        height = rois_feat.shape[2]
        width = rois_feat.shape[3]

        channels = np.array(range(channel_num))
	channels = channels.reshape([self._block_num, -1])
	# print channels.shape

	# print rois_feat.shape
        trans_grid = np.zeros([batch_size, channel_num, 2, height*width])
	# print trans_grid.shape
        for b in range(batch_size):
            for i in range(self._block_num):
		trans_grid[b, channels[i], :, :] = self.generate_grid(bottom[1], len(channels[i]), trans_param[b, i])

        top_ind = self._name_to_top_map['trans_feat']
	# print top[top_ind].data.shape
        top[top_ind].reshape(*(rois_feat.shape))
        self.bilinear_sample(rois_feat, top[top_ind].data, trans_grid)

        # calculate the diff
        ones = np.ones(rois_feat.shape)
        du = np.zeros(rois_feat.shape)
        self.bilinear_sample(ones, du, trans_grid)

        bottom_inx = self._name_to_bottom_map['pool5']
        bottom[bottom_inx].diff[...] = du

        dxs, dys = self.calcu_diff(rois_feat, trans_grid)
        xt = np.array(range(width * height))
	# print xt, batch_size, channel_num
	xt = np.tile(xt, (batch_size, channel_num, 1))
	# print xt.shape
        yt = np.array(range(width * height))
	yt = np.tile(yt, [batch_size, channel_num, 1])
        trans_param_tiled = np.zeros([batch_size, channel_num])
        for b in range(batch_size):
            for i in range(self._block_num):
                trans_param_tiled[b,channels[i]] = trans_param[b,i]
        trans_param_tiled = np.expand_dims(trans_param_tiled, axis=3)

        sin_theta = np.sin(trans_param_tiled)
        cos_theta = np.cos(trans_param_tiled)

	# print 'sin', sin_theta.shape
	# print 'xt', xt.shape
	# print 'dxs', dxs.shape
        dtheta = dxs * (-1 * xt * sin_theta + yt * cos_theta) + dys * (-1* xt * cos_theta - yt * sin_theta)
        # print 'dtheta shape', dtheta.shape
        dtheta = np.mean(dtheta, axis=2)
        # print 'dtheta shape', dtheta.shape

        dtheta_mean = np.zeros([batch_size, self._block_num])
        for i in range(self._block_num):
            dtheta_mean[:, i] = np.mean(dtheta[:, channels[i]], axis=1)

        bottom_inx = self._name_to_bottom_map['trans_param']
        bottom[bottom_inx].diff[...] = dtheta_mean


    def backward(self, top, propagate_down, bottom):
	# print 'bottom', bottom[0].data.shape
	# print 'top', top[0].data.shape
	batch_size = top[0].data.shape[0]
	channels = top[0].data.shape[1]

	bottom_inx = self._name_to_bottom_map['pool5']
	bottom[bottom_inx].diff[...] *= top[0].diff

	bottom_inx = self._name_to_bottom_map['trans_param']
    	diff = np.copy(top[0].diff)
	diff = diff.reshape([batch_size, self._block_num, -1])
	# print 'diff.shape', diff.shape
	dtheta = np.mean(diff, axis=2)
	# print 'dtheta.shape', dtheta.shape
	bottom[bottom_inx].diff[...] *= dtheta


    def reshape(self, bottom, top):
        """Reshaping happens during the call to forward."""
        pass


class ASTNLossLayer(caffe.Layer):
    """optimize the detector to classify foreground objects as the background class"""

    def setup(self, bottom, top):
        """Setup the ASTNDataLayer"""

        # parse the layer parameter string, which must be valid YAML
        layer_params = yaml.load(self.param_str_)

        self._num_classes = layer_params['num_classes']

        self._name_to_bottom_map = {
            'cls_prob': 0,
            'labels': 1
        }

        self._name_to_top_map = {
            'labels': 0
        }

        top[0].reshape(1)


    def forward(self, bottom, top):
        prob_pre = bottom[0].data
        labels = bottom[1].data
	# print 'prob_pre.shape', prob_pre.shape
	# print 'labels.shape', labels.shape

        batch_size = prob_pre.shape[0]
	labels = labels.reshape(batch_size).astype(np.int32)
        loss = -np.log(1 - prob_pre[range(batch_size), labels])
        dloss = 1 / (1 - prob_pre[:, labels])
        for i in range(batch_size):
            if labels[i] == 0:
                loss[i] = 0.0
                dloss[i] = 0.0

        top[0].data[...] = np.sum(loss) / batch_size
        bottom[0].diff[...] = np.sum(dloss) / batch_size


    def backward(self, top, propagate_down, bottom):
        bottom[0].diff[...] *= top[0].diff


    def reshape(self, bottom, top):
        """Reshaping happens during the call to forward."""
        pass


class BlobFetcher(Process):
    """Experimental class for prefetching blobs in a separate process."""
    def __init__(self, queue, roidb, num_classes):
        super(BlobFetcher, self).__init__()
        self._queue = queue
        self._roidb = roidb
        self._num_classes = num_classes
        self._perm = None
        self._cur = 0
        self._shuffle_roidb_inds()
        # fix the random seed for reproducibility
        np.random.seed(cfg.RNG_SEED)

    def _shuffle_roidb_inds(self):
        """Randomly permute the training roidb."""
        # TODO(rbg): remove duplicated code
        self._perm = np.random.permutation(np.arange(len(self._roidb)))
        self._cur = 0

    def _get_next_minibatch_inds(self):
        """Return the roidb indices for the next minibatch."""
        # TODO(rbg): remove duplicated code
        if self._cur + cfg.TRAIN.IMS_PER_BATCH >= len(self._roidb):
            self._shuffle_roidb_inds()

        db_inds = self._perm[self._cur:self._cur + cfg.TRAIN.IMS_PER_BATCH]
        self._cur += cfg.TRAIN.IMS_PER_BATCH
        return db_inds

    def run(self):
        print 'BlobFetcher started'
        while True:
            db_inds = self._get_next_minibatch_inds()
            minibatch_db = [self._roidb[i] for i in db_inds]
            if cfg.TRAIN.USE_OHEM:
                blobs = get_allrois_minibatch(minibatch_db, self._num_classes)
            else:
                blobs = get_minibatch(minibatch_db, self._num_classes)
            self._queue.put(blobs)

"""
    SimplePose for COCO Keypoint, implemented in Chainer.
    Original paper: 'Simple Baselines for Human Pose Estimation and Tracking,' https://arxiv.org/abs/1804.06208.
"""

__all__ = ['SimplePose', 'simplepose_resnet18_coco', 'simplepose_resnet50b_coco', 'simplepose_resnet101b_coco',
           'simplepose_resnet152b_coco', 'simplepose_resneta50b_coco', 'simplepose_resneta101b_coco',
           'simplepose_resneta152b_coco']

import os
import numpy as np
import chainer.functions as F
import chainer.links as L
from chainer import Chain
from chainer.serializers import load_npz
from .common import get_activation_layer, conv1x1, SimpleSequential
from .resnet import resnet18, resnet50b, resnet101b, resnet152b
from .resneta import resneta50b, resneta101b, resneta152b


class DeconvBlock(Chain):
    """
    Deconvolution block with batch normalization and activation.

    Parameters:
    ----------
    in_channels : int
        Number of input channels.
    out_channels : int
        Number of output channels.
    ksize : int or tuple/list of 2 int
        Convolution window size.
    stride : int or tuple/list of 2 int
        Stride of the deconvolution.
    pad : int or tuple/list of 2 int
        Padding value for deconvolution layer.
    dilate : int or tuple/list of 2 int, default 1
        Dilation value for deconvolution layer.
    groups : int, default 1
        Number of groups.
    use_bias : bool, default False
        Whether the layer uses a bias vector.
    use_bn : bool, default True
        Whether to use BatchNorm layer.
    bn_eps : float, default 1e-5
        Small float added to variance in Batch norm.
    activation : function or str or None, default F.relu
        Activation function or name of activation function.
    """
    def __init__(self,
                 in_channels,
                 out_channels,
                 ksize,
                 stride,
                 pad,
                 dilate=1,
                 groups=1,
                 use_bias=False,
                 use_bn=True,
                 bn_eps=1e-5,
                 activation=(lambda: F.relu),
                 **kwargs):
        super(DeconvBlock, self).__init__(**kwargs)
        self.activate = (activation is not None)
        self.use_bn = use_bn

        with self.init_scope():
            self.conv = L.Deconvolution2D(
                in_channels=in_channels,
                out_channels=out_channels,
                ksize=ksize,
                stride=stride,
                pad=pad,
                nobias=(not use_bias),
                dilate=dilate,
                groups=groups)
            if self.use_bn:
                self.bn = L.BatchNormalization(
                    size=out_channels,
                    eps=bn_eps)
            if self.activate:
                self.activ = get_activation_layer(activation)

    def __call__(self, x):
        x = self.conv(x)
        if self.use_bn:
            x = self.bn(x)
        if self.activate:
            x = self.activ(x)
        return x


class SimplePose(Chain):
    """
    SimplePose model from 'Simple Baselines for Human Pose Estimation and Tracking,' https://arxiv.org/abs/1804.06208.

    Parameters:
    ----------
    backbone : nn.Sequential
        Feature extractor.
    backbone_out_channels : int
        Number of output channels for the backbone.
    channels : list of int
        Number of output channels for each decoder unit.
    return_heatmap : bool, default False
        Whether to return only heatmap.
    in_channels : int, default 3
        Number of input channels.
    in_size : tuple of two ints, default (256, 192)
        Spatial size of the expected input image.
    keypoints : int, default 17
        Number of keypoints.
    """
    def __init__(self,
                 backbone,
                 backbone_out_channels,
                 channels,
                 return_heatmap=False,
                 in_channels=3,
                 in_size=(256, 192),
                 keypoints=17,
                 **kwargs):
        super(SimplePose, self).__init__(**kwargs)
        assert (in_channels == 3)
        self.in_size = in_size
        self.keypoints = keypoints
        self.return_heatmap = return_heatmap
        self.out_size = (in_size[0] // 4, in_size[1] // 4)

        with self.init_scope():
            self.backbone = backbone

            in_channels = backbone_out_channels
            self.decoder = SimpleSequential()
            with self.decoder.init_scope():
                for i, out_channels in enumerate(channels):
                    setattr(self.decoder, "unit{}".format(i + 1), DeconvBlock(
                        in_channels=in_channels,
                        out_channels=out_channels,
                        ksize=4,
                        stride=2,
                        pad=1))
                    in_channels = out_channels

            self.final_block = conv1x1(
                in_channels=in_channels,
                out_channels=keypoints,
                use_bias=True)

    def __call__(self, x):
        x = self.backbone(x)
        x = self.decoder(x)
        heatmap = self.final_block(x)
        if self.return_heatmap:
            return heatmap

        vector_dim = 2
        batch = heatmap.shape[0]
        heatmap_vector = F.reshape(heatmap, shape=(batch, self.keypoints, -1))
        indices = F.cast(F.expand_dims(F.argmax(heatmap_vector, axis=vector_dim), axis=vector_dim), np.float32)
        scores = F.max(heatmap_vector, axis=vector_dim, keepdims=True)
        scores_mask = (scores.array > 0.0).astype(np.float32)
        keys_x = (indices.array % self.out_size[1]) * scores_mask
        keys_y = (indices.array // self.out_size[1]) * scores_mask
        keypoints = F.concat((keys_x, keys_y, scores), axis=vector_dim).array
        for b in range(batch):
            for k in range(self.keypoints):
                hm = heatmap[b, k, :, :].array
                px = int(keys_x[b, k])
                py = int(keys_y[b, k])
                if (1 < px < self.out_size[1] - 1) and (1 < py < self.out_size[0] - 1):
                    keypoints[b, k, 0] += np.sign(hm[py, px + 1] - hm[py, px - 1]) * 0.25
                    keypoints[b, k, 1] += np.sign(hm[py + 1, px] - hm[py - 1, px]) * 0.25
        return keypoints


def get_simplepose(backbone,
                   backbone_out_channels,
                   keypoints,
                   model_name=None,
                   pretrained=False,
                   root=os.path.join("~", ".chainer", "models"),
                   **kwargs):
    """
    Create SimplePose model with specific parameters.

    Parameters:
    ----------
    backbone : nn.Sequential
        Feature extractor.
    backbone_out_channels : int
        Number of output channels for the backbone.
    keypoints : int
        Number of keypoints.
    model_name : str or None, default None
        Model name for loading pretrained model.
    pretrained : bool, default False
        Whether to load the pretrained weights for model.
    root : str, default '~/.chainer/models'
        Location for keeping the model parameters.
    """
    channels = [256, 256, 256]

    net = SimplePose(
        backbone=backbone,
        backbone_out_channels=backbone_out_channels,
        channels=channels,
        keypoints=keypoints,
        **kwargs)

    if pretrained:
        if (model_name is None) or (not model_name):
            raise ValueError("Parameter `model_name` should be properly initialized for loading pretrained model.")
        from .model_store import get_model_file
        load_npz(
            file=get_model_file(
                model_name=model_name,
                local_model_store_dir_path=root),
            obj=net)

    return net


def simplepose_resnet18_coco(pretrained_backbone=False, keypoints=17, **kwargs):
    """
    SimplePose model on the base of ResNet-18 for COCO Keypoint from 'Simple Baselines for Human Pose Estimation and
    Tracking,' https://arxiv.org/abs/1804.06208.

    Parameters:
    ----------
    pretrained_backbone : bool, default False
        Whether to load the pretrained weights for feature extractor.
    keypoints : int, default 17
        Number of keypoints.
    pretrained : bool, default False
        Whether to load the pretrained weights for model.
    root : str, default '~/.chainer/models'
        Location for keeping the model parameters.
    """
    backbone = resnet18(pretrained=pretrained_backbone).features
    del backbone.final_pool
    return get_simplepose(backbone=backbone, backbone_out_channels=512, keypoints=keypoints,
                          model_name="simplepose_resnet18_coco", **kwargs)


def simplepose_resnet50b_coco(pretrained_backbone=False, keypoints=17, **kwargs):
    """
    SimplePose model on the base of ResNet-50b for COCO Keypoint from 'Simple Baselines for Human Pose Estimation and
    Tracking,' https://arxiv.org/abs/1804.06208.

    Parameters:
    ----------
    pretrained_backbone : bool, default False
        Whether to load the pretrained weights for feature extractor.
    keypoints : int, default 17
        Number of keypoints.
    pretrained : bool, default False
        Whether to load the pretrained weights for model.
    root : str, default '~/.chainer/models'
        Location for keeping the model parameters.
    """
    backbone = resnet50b(pretrained=pretrained_backbone).features
    del backbone.final_pool
    return get_simplepose(backbone=backbone, backbone_out_channels=2048, keypoints=keypoints,
                          model_name="simplepose_resnet50b_coco", **kwargs)


def simplepose_resnet101b_coco(pretrained_backbone=False, keypoints=17, **kwargs):
    """
    SimplePose model on the base of ResNet-101b for COCO Keypoint from 'Simple Baselines for Human Pose Estimation
    and Tracking,' https://arxiv.org/abs/1804.06208.

    Parameters:
    ----------
    pretrained_backbone : bool, default False
        Whether to load the pretrained weights for feature extractor.
    keypoints : int, default 17
        Number of keypoints.
    pretrained : bool, default False
        Whether to load the pretrained weights for model.
    root : str, default '~/.chainer/models'
        Location for keeping the model parameters.
    """
    backbone = resnet101b(pretrained=pretrained_backbone).features
    del backbone.final_pool
    return get_simplepose(backbone=backbone, backbone_out_channels=2048, keypoints=keypoints,
                          model_name="simplepose_resnet101b_coco", **kwargs)


def simplepose_resnet152b_coco(pretrained_backbone=False, keypoints=17, **kwargs):
    """
    SimplePose model on the base of ResNet-152b for COCO Keypoint from 'Simple Baselines for Human Pose Estimation
    and Tracking,' https://arxiv.org/abs/1804.06208.

    Parameters:
    ----------
    pretrained_backbone : bool, default False
        Whether to load the pretrained weights for feature extractor.
    keypoints : int, default 17
        Number of keypoints.
    pretrained : bool, default False
        Whether to load the pretrained weights for model.
    root : str, default '~/.chainer/models'
        Location for keeping the model parameters.
    """
    backbone = resnet152b(pretrained=pretrained_backbone).features
    del backbone.final_pool
    return get_simplepose(backbone=backbone, backbone_out_channels=2048, keypoints=keypoints,
                          model_name="simplepose_resnet152b_coco", **kwargs)


def simplepose_resneta50b_coco(pretrained_backbone=False, keypoints=17, **kwargs):
    """
    SimplePose model on the base of ResNet(A)-50b for COCO Keypoint from 'Simple Baselines for Human Pose Estimation
    and Tracking,' https://arxiv.org/abs/1804.06208.

    Parameters:
    ----------
    pretrained_backbone : bool, default False
        Whether to load the pretrained weights for feature extractor.
    keypoints : int, default 17
        Number of keypoints.
    pretrained : bool, default False
        Whether to load the pretrained weights for model.
    root : str, default '~/.chainer/models'
        Location for keeping the model parameters.
    """
    backbone = resneta50b(pretrained=pretrained_backbone).features
    del backbone.final_pool
    return get_simplepose(backbone=backbone, backbone_out_channels=2048, keypoints=keypoints,
                          model_name="simplepose_resneta50b_coco", **kwargs)


def simplepose_resneta101b_coco(pretrained_backbone=False, keypoints=17, **kwargs):
    """
    SimplePose model on the base of ResNet(A)-101b for COCO Keypoint from 'Simple Baselines for Human Pose Estimation
    and Tracking,' https://arxiv.org/abs/1804.06208.

    Parameters:
    ----------
    pretrained_backbone : bool, default False
        Whether to load the pretrained weights for feature extractor.
    keypoints : int, default 17
        Number of keypoints.
    pretrained : bool, default False
        Whether to load the pretrained weights for model.
    root : str, default '~/.chainer/models'
        Location for keeping the model parameters.
    """
    backbone = resneta101b(pretrained=pretrained_backbone).features
    del backbone.final_pool
    return get_simplepose(backbone=backbone, backbone_out_channels=2048, keypoints=keypoints,
                          model_name="simplepose_resneta101b_coco", **kwargs)


def simplepose_resneta152b_coco(pretrained_backbone=False, keypoints=17, **kwargs):
    """
    SimplePose model on the base of ResNet(A)-152b for COCO Keypoint from 'Simple Baselines for Human Pose Estimation
    and Tracking,' https://arxiv.org/abs/1804.06208.

    Parameters:
    ----------
    pretrained_backbone : bool, default False
        Whether to load the pretrained weights for feature extractor.
    keypoints : int, default 17
        Number of keypoints.
    pretrained : bool, default False
        Whether to load the pretrained weights for model.
    root : str, default '~/.chainer/models'
        Location for keeping the model parameters.
    """
    backbone = resneta152b(pretrained=pretrained_backbone).features
    del backbone.final_pool
    return get_simplepose(backbone=backbone, backbone_out_channels=2048, keypoints=keypoints,
                          model_name="simplepose_resneta152b_coco", **kwargs)


def _test():
    import numpy as np
    import chainer

    chainer.global_config.train = False

    in_size = (256, 192)
    keypoints = 17
    return_heatmap = False
    pretrained = False

    models = [
        simplepose_resnet18_coco,
        simplepose_resnet50b_coco,
        simplepose_resnet101b_coco,
        simplepose_resnet152b_coco,
        simplepose_resneta50b_coco,
        simplepose_resneta101b_coco,
        simplepose_resneta152b_coco,
    ]

    for model in models:

        net = model(pretrained=pretrained, in_size=in_size, return_heatmap=return_heatmap)
        weight_count = net.count_params()
        print("m={}, {}".format(model.__name__, weight_count))
        assert (model != simplepose_resnet18_coco or weight_count == 15376721)
        assert (model != simplepose_resnet50b_coco or weight_count == 33999697)
        assert (model != simplepose_resnet101b_coco or weight_count == 52991825)
        assert (model != simplepose_resnet152b_coco or weight_count == 68635473)
        assert (model != simplepose_resneta50b_coco or weight_count == 34018929)
        assert (model != simplepose_resneta101b_coco or weight_count == 53011057)
        assert (model != simplepose_resneta152b_coco or weight_count == 68654705)

        batch = 14
        x = np.random.rand(batch, 3, in_size[0], in_size[1]).astype(np.float32)
        y = net(x)
        assert ((y.shape[0] == batch) and (y.shape[1] == keypoints))
        if return_heatmap:
            assert ((y.shape[2] == x.shape[2] // 4) and (y.shape[3] == x.shape[3] // 4))
        else:
            assert (y.shape[2] == 3)


if __name__ == "__main__":
    _test()

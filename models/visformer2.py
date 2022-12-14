"""
MindSpore implementation of `Visformer`.
Refer to: Visformer: The Vision-friendly Transformer
"""
from typing import List
import numpy as np

import mindspore as ms
from mindspore import nn, ops, Tensor
from mindspore.common.initializer import initializer, HeNormal, Constant, TruncatedNormal

from .utils import load_pretrained, _ntuple
from .layers import Identity, GlobalAvgPooling, DropPath
from .registry import register_model

__all__ = [
    'visformer_tiny',
    'visformer_small',
    'visformer_tiny_v2',
    'visformer_small_v2'
]


def _cfg(url='', **kwargs):
    return {
        'url': url,
        'num_classes': 1000,
        'first_conv': '', 'classifier': '',
        **kwargs
    }


default_cfgs = {
    'visformer_small': _cfg(url=''),
    'visformer_tiny': _cfg(url=''),
    'visformer_tiny_v2': _cfg(url=''),
    'visformer_small_v2': _cfg(url='')
}

to_2tuple = _ntuple(2)


class LayerNorm(nn.LayerNorm):
    """LayerNorm"""

    def __init__(self, num_channels):
        super(LayerNorm, self).__init__([num_channels, 1, 1])

    def construct(self, x):
        return ops.LayerNorm(self.normalized_shape)(x, self.gamma, self.beta)


class BatchNorm(nn.Cell):
    """BatchNorm"""

    def __init__(self, dim):
        super(BatchNorm, self).__init__()
        self.bn = nn.BatchNorm2d(dim, eps=1e-5, momentum=0.9)

    def construct(self, x):
        return self.bn(x)


class Mlp(nn.Cell):

    def __init__(self, in_features: int,
                 hidden_features: int = None,
                 out_features: int = None,
                 act_layer: nn.Cell = nn.GELU,
                 drop: float = 0.,
                 group: int = 8,
                 spatial_conv: bool = False):
        super(Mlp, self).__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.in_features = in_features
        self.out_features = out_features
        self.spatial_conv = spatial_conv
        if self.spatial_conv:
            if group < 2:
                hidden_features = in_features * 5 // 6
            else:
                hidden_features = in_features * 2
        self.hidden_features = hidden_features
        self.group = group
        self.drop = nn.Dropout(1 - drop)
        self.conv1 = nn.Conv2d(in_features, hidden_features, 1, 1, pad_mode='pad', padding=0)
        self.act1 = act_layer()
        if self.spatial_conv:
            self.conv2 = nn.Conv2d(hidden_features, hidden_features, 3, 1, pad_mode='pad', padding=1, group=self.group)
            self.act2 = act_layer()
        self.conv3 = nn.Conv2d(hidden_features, out_features, 1, 1, pad_mode='pad', padding=0)

    def construct(self, x: Tensor):
        x = self.conv1(x)
        x = self.act1(x)
        x = self.drop(x)

        if self.spatial_conv:
            x = self.conv2(x)
            x = self.act2(x)

        x = self.conv3(x)
        x = self.drop(x)
        return x


class WindowAttention(nn.Cell):
    r""" Window based multi-head self attention (W-MSA) module with relative position bias.
    It supports both of shifted and non-shifted window.
    Args:
        dim (int): Number of input channels.
        window_size (tuple[int]): The height and width of the window.
        num_heads (int): Number of attention heads.
        qkv_bias (bool, optional):  If True, add a learnable bias to query, key, value. Default: True
        qk_scale (float | None, optional): Override default qk scale of head_dim ** -0.5 if set
        attn_drop (float, optional): Dropout ratio of attention weight. Default: 0.0
        proj_drop (float, optional): Dropout ratio of output. Default: 0.0
    """

    def __init__(self,
                 dim: int,
                 window_size: tuple,
                 num_heads: int = 8,
                 head_dim_ratio: float = 1.,
                 qkv_bias: bool = False,
                 qk_scale: float = None,
                 attn_drop: float = 0.,
                 proj_drop: float = 0.):
        super(WindowAttention, self).__init__()
        self.dim = dim
        self.window_size = window_size  # Wh, Ww
        self.num_heads = num_heads
        self.head_dim = int(dim // num_heads * head_dim_ratio)

        bias_table_shape = ((2 * window_size[0] - 1) * (2 * window_size[1] - 1), num_heads)
        self.relative_postion_bias_table = ms.Parameter(ops.zeros(bias_table_shape, ms.float32))  # 2*Wh-1 * 2*Ww-1, nH

        coords_h = ms.numpy.arange(self.window_size[0])
        coords_w = ms.numpy.arange(self.window_size[1])
        coords = ops.stack(ops.meshgrid((coords_h, coords_w)))  # 2, Wh, Ww
        coords_flatten = ops.flatten(coords)
        relative_coords = coords_flatten[:, :, None] - coords_flatten[:, None, :]  # 2, Wh*Ww, Wh*Ww
        relative_coords = ops.transpose(relative_coords, (1, 2, 0))  # Wh*Ww, Wh*Ww, 2
        relative_coords[:, :, 0] += self.window_size[0] - 1  # shift to start from 0
        relative_coords[:, :, 1] += self.window_size[1] - 1
        relative_coords[:, :, 0] *= 2 * self.window_size[1] - 1
        relative_position_index = relative_coords.sum(axis=-1)  # Wh*Ww, Wh*Ww
        self.relative_position_index = ms.Parameter(relative_position_index, requires_grad=False)

        qk_scale_factor = qk_scale if qk_scale is not None else -0.25
        self.scale = self.head_dim ** qk_scale_factor

        self.qkv = nn.Conv2d(dim, self.head_dim * num_heads * 3, 1, 1, pad_mode='pad', padding=0, has_bias=qkv_bias)
        self.attn_drop = nn.Dropout(1 - attn_drop)
        self.proj = nn.Conv2d(self.head_dim * self.num_heads, dim, 1, 1, pad_mode='pad', padding=0)
        self.proj_drop = nn.Dropout(1 - proj_drop)

        self.relative_postion_bias_table.set_data(initializer(TruncatedNormal(0.02), bias_table_shape, ms.float32))
        self.softmax = nn.Softmax(axis=-1)

    def construct(self, x):
        B, C, H, W = x.shape
        x = self.qkv(x)
        qkv = ops.reshape(x, (B, 3, self.num_heads, self.head_dim, H * W))
        qkv = qkv.transpose((1, 0, 2, 4, 3))
        q, k, v = qkv[0], qkv[1], qkv[2]

        attn = ops.matmul(q * self.scale, k.transpose(0, 1, 3, 2) * self.scale)
        relative_position_bias = self.relative_postion_bias_table[self.relative_position_index.view(-1)].view(
            self.window_size[0] * self.window_size[1], self.window_size[0] * self.window_size[1], -1)  # Wh*Ww,Wh*Ww,nH
        relative_position_bias = ops.transpose(relative_position_bias, (2, 0, 1))  # nH, Wh*Ww, Wh*Ww
        attn = attn + relative_position_bias.expand_dims(0)
        attn = self.softmax(attn)
        attn = self.attn_drop(attn)

        x = ops.matmul(attn, v)
        x = x.transpose((0, 1, 3, 2)).reshape((B, -1, H, W))
        x = self.proj(x)
        x = self.proj_drop(x)
        return x


class Block(nn.Cell):

    def __init__(self,
                 dim: int,
                 input_resolution: tuple,
                 num_heads: int,
                 window_size: int = 7,
                 shift_size: int = 0,
                 head_dim_ratio: float = 1.,
                 mlp_ratio: float = 4.,
                 qkv_bias: bool = False,
                 qk_scale: float = None,
                 drop: float = 0.,
                 attn_drop: float = 0.,
                 drop_path: float = 0.,
                 act_layer: nn.Cell = nn.GELU,
                 norm_layer: BatchNorm = BatchNorm,
                 group: int = 8,
                 attn_disabled: bool = False,
                 spatial_conv: bool = False):
        super(Block, self).__init__()
        self.attn_disabled = attn_disabled
        self.spatial_conv = spatial_conv
        self.input_resolution = input_resolution
        self.window_size = window_size
        self.shift_size = shift_size

        if min(self.input_resolution) <= self.window_size:
            self.shift_size = 0
            self.window_size = min(self.input_resolution)

        self.drop_path = DropPath(drop_path) if drop_path > 0. else Identity()
        if not attn_disabled:
            self.norm1 = norm_layer(dim)
            self.attn = WindowAttention(dim, window_size=to_2tuple(self.window_size), num_heads=num_heads,
                                        head_dim_ratio=head_dim_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                                        attn_drop=attn_drop, proj_drop=drop)
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop,
                       group=group, spatial_conv=spatial_conv)

    def construct(self, x: Tensor):
        if not self.attn_disabled:
            x = x + self.drop_path(self.attn(self.norm1(x)))
        x = x + self.drop_path(self.mlp(self.norm2(x)))
        return x


class PatchEmbed(nn.Cell):

    def __init__(self,
                 img_size: int = 224,
                 patch_size: int = 16,
                 in_chans: int = 3,
                 embed_dim: int = 768,
                 norm_layer: bool = None):
        super(PatchEmbed, self).__init__()
        img_size = to_2tuple(img_size)
        patch_size = to_2tuple(patch_size)
        num_patches = (img_size[1] // patch_size[1]) * (img_size[0] // patch_size[0])

        self.img_size = img_size
        self.patch_size = patch_size
        self.num_patches = num_patches
        self.proj = nn.Conv2d(in_chans, embed_dim, kernel_size=patch_size, stride=patch_size, pad_mode='pad', padding=0,
                              has_bias=True)
        self.norm_pe = norm_layer is not None
        if self.norm_pe:
            self.norm = norm_layer(embed_dim)

    def construct(self, x: Tensor):
        x = self.proj(x)
        if self.norm_pe:
            x = self.norm(x)
        return x


class Visformer(nn.Cell):

    def __init__(self,
                 img_size: int = 224,
                 init_channels: int = 32,
                 num_classes: int = 1000,
                 embed_dim: int = 384,
                 depth: List[int] = None,
                 num_heads: List[int] = 6,
                 mlp_ratio: float = 4.,
                 qkv_bias: bool = False,
                 qk_scale: float = None,
                 drop_rate: float = 0.,
                 attn_drop_rate: float = 0.,
                 drop_path_rate: float = 0.1,
                 norm_layer: BatchNorm = BatchNorm,
                 attn_stage: str = '1111',
                 pos_embed: bool = True,
                 spatial_conv: str = '1111',
                 group: int = 8,
                 pool: bool = True,
                 conv_init: bool = False,
                 embedding_norm: bool = None):
        super(Visformer, self).__init__()
        self.num_classes = num_classes
        self.num_features = self.embed_dim = embed_dim
        self.init_channels = init_channels
        self.img_size = img_size
        self.pool = pool
        self.conv_init = conv_init
        self.depth = depth
        assert (isinstance(depth, list) or isinstance(depth, tuple)) and len(depth) == 4
        if not (isinstance(num_heads, list) or isinstance(num_heads, tuple)):
            num_heads = [num_heads] * 4

        self.pos_embed = pos_embed
        dpr = np.linspace(0, drop_path_rate, sum(depth)).tolist()

        self.stem = nn.SequentialCell([
            nn.Conv2d(3, self.init_channels, 7, 2, pad_mode='pad', padding=3),
            BatchNorm(self.init_channels),
            nn.ReLU()
        ])
        img_size //= 2

        self.pos_drop = nn.Dropout(1 - drop_rate)
        # stage0
        self.patch_embed0 = PatchEmbed(img_size=img_size, patch_size=2, in_chans=self.init_channels,
                                       embed_dim=embed_dim // 4, norm_layer=embedding_norm)
        img_size //= 2
        if self.pos_embed:
            self.pos_embed0 = ms.Parameter(ops.zeros((1, embed_dim // 4, img_size, img_size), ms.float32))
        self.stage0 = nn.CellList([
            Block(dim=embed_dim // 4, input_resolution=(img_size, img_size), window_size=56,
                  shift_size=0 if(i % 2 == 0) else 28, num_heads=num_heads[0], head_dim_ratio=0.25,
                  mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale, drop=drop_rate,
                  attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer, group=group,
                  attn_disabled=(attn_stage[0] == '0'), spatial_conv=(spatial_conv[0] == '1'))
            for i in range(0, sum(depth[:1]))
        ])

        self.patch_embed1 = PatchEmbed(img_size=img_size, patch_size=2, in_chans=embed_dim // 4,
                                       embed_dim=embed_dim // 2, norm_layer=embedding_norm)
        img_size //= 2
        if self.pos_embed:
            self.pos_embed1 = ms.Parameter(ops.zeros((1, embed_dim // 2, img_size, img_size), ms.float32))

        self.stage1 = nn.CellList([
            Block(dim=embed_dim // 2, input_resolution=(img_size, img_size), window_size=28,
                  shift_size=0 if (i % 2 == 0) else 14, num_heads=num_heads[1], head_dim_ratio=0.5,
                  mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale, drop=drop_rate,
                  attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer, group=group,
                  attn_disabled=(attn_stage[1] == '0'), spatial_conv=(spatial_conv[1] == '1'))
            for i in range(sum(depth[:1]), sum(depth[:2]))
        ])

        # stage2
        self.patch_embed2 = PatchEmbed(img_size=img_size, patch_size=2, in_chans=embed_dim // 2, embed_dim=embed_dim,
                                       norm_layer=embedding_norm)
        img_size //= 2
        if self.pos_embed:
            self.pos_embed2 = ms.Parameter(ops.zeros((1, embed_dim, img_size, img_size), ms.float32))

        self.stage2 = nn.CellList([
            Block(dim=embed_dim, input_resolution=(img_size, img_size), window_size=14,
                  shift_size=0 if (i % 2 == 0) else 7, num_heads=num_heads[2], head_dim_ratio=1.0,
                  mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale, drop=drop_rate,
                  attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer, group=group,
                  attn_disabled=(attn_stage[2] == '0'), spatial_conv=(spatial_conv[2] == '1'))
            for i in range(sum(depth[:2]), sum(depth[:3]))
        ])

        # stage3
        self.patch_embed3 = PatchEmbed(img_size=img_size, patch_size=2, in_chans=embed_dim, embed_dim=embed_dim * 2,
                                       norm_layer=embedding_norm)
        img_size //= 2
        if self.pos_embed:
            self.pos_embed3 = ms.Parameter(ops.zeros((1, embed_dim * 2, img_size, img_size), ms.float32))

        self.stage3 = nn.CellList([
            Block(dim=embed_dim*2, input_resolution=(img_size, img_size), window_size=7,
                  shift_size=0 if (i % 2 == 0) else 3, num_heads=num_heads[3], head_dim_ratio=1.0,
                  mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale, drop=drop_rate,
                  attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer, group=group,
                  attn_disabled=(attn_stage[3] == '0'), spatial_conv=(spatial_conv[3] == '1'))
            for i in range(sum(depth[:3]), sum(depth[:4]))
        ])

        # head
        if self.pool:
            self.global_pooling = GlobalAvgPooling()

        self.norm = norm_layer(embed_dim * 2)
        self.head = nn.Dense(embed_dim * 2, num_classes)

        # weight init
        if self.pos_embed:
            self.pos_embed0.set_data(initializer(TruncatedNormal(0.02),
                                                 self.pos_embed0.shape, self.pos_embed0.dtype))
            self.pos_embed1.set_data(initializer(TruncatedNormal(0.02),
                                                 self.pos_embed1.shape, self.pos_embed1.dtype))
            self.pos_embed2.set_data(initializer(TruncatedNormal(0.02),
                                                 self.pos_embed2.shape, self.pos_embed2.dtype))
            self.pos_embed3.set_data(initializer(TruncatedNormal(0.02),
                                                 self.pos_embed3.shape, self.pos_embed3.dtype))
        self._initialize_weights()

    def _initialize_weights(self):
        for _, cell in self.cells_and_names():
            if isinstance(cell, nn.Dense):
                cell.weight.set_data(initializer(TruncatedNormal(0.02), cell.weight.shape, cell.weight.dtype))
                if cell.bias is not None:
                    cell.bias.set_data(initializer(Constant(0), cell.bias.shape, cell.bias.dtype))
            elif isinstance(cell, nn.LayerNorm):
                cell.beta.set_data(initializer(Constant(0), cell.beta.shape, cell.beta.dtype))
                cell.gamma.set_data(initializer(Constant(1), cell.gamma.shape, cell.gamma.dtype))
            elif isinstance(cell, nn.BatchNorm2d):
                cell.beta.set_data(initializer(Constant(0), cell.beta.shape, cell.beta.dtype))
                cell.gamma.set_data(initializer(Constant(1), cell.gamma.shape, cell.gamma.dtype))
            elif isinstance(cell, nn.Conv2d):
                if self.conv_init:
                    cell.weight.set_data(initializer(HeNormal(mode='fan_out', nonlinearity='relu'), cell.weight.shape,
                                                     cell.weight.dtype))
                else:
                    cell.weight.set_data(initializer(TruncatedNormal(0.02), cell.weight.shape, cell.weight.dtype))
                if cell.bias is not None:
                    cell.bias.set_data(initializer(Constant(0), cell.bias.shape, cell.bias.dtype))

    def construct(self, x: Tensor):
        x = self.stem(x)

        # stage 0
        x = self.patch_embed0(x)
        if self.pos_embed:
            x = x + self.pos_embed0
            x = self.pos_drop(x)
        for b in self.stage0:
            x = b(x)

        # stage 1
        x = self.patch_embed1(x)
        if self.pos_embed:
            x = x + self.pos_embed1
            x = self.pos_drop(x)
        for b in self.stage1:
            x = b(x)

        # stage 2
        x = self.patch_embed2(x)
        if self.pos_embed:
            x = x + self.pos_embed2
            x = self.pos_drop(x)
        for b in self.stage2:
            x = b(x)

        # stage 3
        x = self.patch_embed3(x)
        if self.pos_embed:
            x = x + self.pos_embed3
            x = self.pos_drop(x)
        for b in self.stage3:
            x = b(x)

        # head
        x = self.norm(x)
        if self.pool:
            x = self.global_pooling(x)
        else:
            x = x[:, :, 0, 0]
        x = self.head(x.view(x.shape[0], -1))
        return x


@register_model
def visformer_tiny(pretrained: bool = False,
                   num_classes: int = 1000,
                   in_channels: int = 3,
                   **kwargs):
    default_cfg = default_cfgs['visformer_tiny']
    model = Visformer(img_size=224, init_channels=16, embed_dim=192, depth=[0, 7, 4, 4], num_heads=[3, 3, 3, 3],
                      mlp_ratio=4., group=8, attn_stage='0011', spatial_conv='1100', norm_layer=BatchNorm,
                      drop_path_rate=0.03, conv_init=True, embedding_norm=BatchNorm, **kwargs)
    if pretrained:
        load_pretrained(model, default_cfg, num_classes=num_classes, in_channels=in_channels)

    return model


@register_model
def visformer_small(pretrained: bool = False,
                    num_classes: int = 1000,
                    in_channels: int = 3,
                    **kwargs):
    default_cfg = default_cfgs['visformer_small']
    model = Visformer(img_size=224, init_channels=32, embed_dim=384, depth=[0, 7, 4, 4], num_heads=[6, 6, 6, 6],
                      mlp_ratio=4., group=8, attn_stage='0011', spatial_conv='1100', norm_layer=BatchNorm,
                      conv_init=True, embedding_norm=BatchNorm, **kwargs)
    if pretrained:
        load_pretrained(model, default_cfg, num_classes=num_classes, in_channels=in_channels)
    return model


@register_model
def visformer_small_v2(pretrained: bool = False,
                       num_classes: int = 1000,
                       in_channels: int = 3,
                       **kwargs):
    default_cfg = default_cfgs['visformer_small_v2']
    model = Visformer(img_size=224, init_channels=32, embed_dim=256, depth=[1, 10, 14, 3], num_heads=[2, 4, 8, 16],
                      mlp_ratio=4., qk_scale=-0.5, group=8, attn_stage='0011', spatial_conv='1100',
                      norm_layer=BatchNorm, conv_init=True, embedding_norm=BatchNorm, **kwargs)
    if pretrained:
        load_pretrained(model, default_cfg, num_classes=num_classes, in_channels=in_channels)
    return model


@register_model
def visformer_tiny_v2(pretrained: bool = False,
                      num_classes: int = 1000,
                      in_channels: int = 3,
                      **kwargs):
    default_cfg = default_cfgs['visformer_tiny_v2']
    model = Visformer(img_size=224, init_channels=24, embed_dim=192, depth=[1, 4, 6, 3], num_heads=[1, 3, 6, 12],
                      mlp_ratio=4., qk_scale=-0.5, group=8, attn_stage='0011', spatial_conv='1100',
                      norm_layer=BatchNorm, drop_path_rate=0.03, conv_init=True, embedding_norm=BatchNorm, **kwargs)
    if pretrained:
        load_pretrained(model, default_cfg, num_classes=num_classes, in_channels=in_channels)
    return model
"""
MIT License

Copyright (c) 2020 Hyeonki Hong <hhk7734@gmail.com>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import tensorflow as tf
from tensorflow.keras import backend, layers, Model

from .common import YOLOConv2D
from .backbone import CSPDarknet53


class Decode(Model):
    def __init__(self, anchors, cell_width: int, num_classes: int, xyscale):
        super(Decode, self).__init__()
        self.anchors = anchors
        self.cell_width = cell_width
        self.num_classes = num_classes
        self.xyscale = xyscale

        self.reshape0 = layers.Reshape((-1,))
        self.concatenate = layers.Concatenate(axis=-1)
        self.reshape1 = layers.Reshape((-1,))

    def build(self, input_shape):
        self.reshape0.target_shape = (
            input_shape[1],
            input_shape[1],
            3,
            5 + self.num_classes,
        )

        """
        grid(1, i, j, 3, 2) => grid top left coordinates
        [
            [ [[0, 0]], [[1, 0]], [[2, 0]], ...],
            [ [[0, 1]], [[1, 1]], [[2, 1]], ...],
        ]
        """
        self.xy_grid = tf.stack(
            tf.meshgrid(tf.range(input_shape[1]), tf.range(input_shape[1])),
            axis=-1,
        )  # size i, j, 2
        self.xy_grid = tf.reshape(
            self.xy_grid, (1, input_shape[1], input_shape[1], 1, 2)
        )
        self.xy_grid = tf.tile(self.xy_grid, [1, 1, 1, 3, 1])
        self.xy_grid = tf.cast(self.xy_grid, tf.float32)
        self.reshape1.target_shape = (
            input_shape[1] * input_shape[1] * 3,
            5 + self.num_classes,
        )

    def call(self, x, training: bool = False):
        x = self.reshape0(x)
        dxdy, wh, score, classes = tf.split(
            x, (2, 2, 1, self.num_classes), axis=-1
        )

        """
        x = f(dx) + left_x
        y = f(dy) + top_y
        w = anchor_w * exp(w)
        h = anchor_h * exp(h)
        """
        dxdy = tf.keras.activations.sigmoid(dxdy)
        xy = (
            (dxdy - 0.5) * self.xyscale + 0.5 + self.xy_grid
        ) * self.cell_width

        wh = self.anchors * backend.exp(wh)

        if not training:
            score = tf.keras.activations.sigmoid(score)
            classes = tf.keras.activations.sigmoid(classes)

        x = self.concatenate([xy, wh, score, classes])
        x = self.reshape1(x)
        return x


class YOLOv4(Model):
    """
    Path Aggregation Network(PAN)
    Spatial Attention Module(SAM)
    Bounding Box(BBox)

    bboxes: (batch, *grid(x, x), num_anchors, (xywh + score + num_classes))
    Each grid cell has anchors.
    Each anchor has (x, y, w, h, score, c0, c1, c2, ...)

    inference: x, y, w, h, sig(s), sig(c0), ...
    training:  x, y, w, h, s,      c0,      ...

    @return (batch, candidates,(xywh + score + num_classes))
    """

    def __init__(self, anchors, num_classes: int, xyscales):
        super(YOLOv4, self).__init__()
        self.csp_darknet53 = CSPDarknet53()

        self.conv78 = YOLOConv2D(filters=256, kernel_size=1, activation="leaky")
        self.upSampling78 = layers.UpSampling2D()
        self.conv79 = YOLOConv2D(filters=256, kernel_size=1, activation="leaky")
        self.concat78_79 = layers.Concatenate(axis=-1)

        self.conv80 = YOLOConv2D(filters=256, kernel_size=1, activation="leaky")
        self.conv81 = YOLOConv2D(filters=512, kernel_size=3, activation="leaky")
        self.conv82 = YOLOConv2D(filters=256, kernel_size=1, activation="leaky")
        self.conv83 = YOLOConv2D(filters=512, kernel_size=3, activation="leaky")
        self.conv84 = YOLOConv2D(filters=256, kernel_size=1, activation="leaky")

        self.conv85 = YOLOConv2D(filters=128, kernel_size=1, activation="leaky")
        self.upSampling85 = layers.UpSampling2D()
        self.conv86 = YOLOConv2D(filters=128, kernel_size=1, activation="leaky")
        self.concat85_86 = layers.Concatenate(axis=-1)

        self.conv87 = YOLOConv2D(filters=128, kernel_size=1, activation="leaky")
        self.conv88 = YOLOConv2D(filters=256, kernel_size=3, activation="leaky")
        self.conv89 = YOLOConv2D(filters=128, kernel_size=1, activation="leaky")
        self.conv90 = YOLOConv2D(filters=256, kernel_size=3, activation="leaky")
        self.conv91 = YOLOConv2D(filters=128, kernel_size=1, activation="leaky")

        self.conv92 = YOLOConv2D(filters=256, kernel_size=3, activation="leaky")
        self.conv93 = YOLOConv2D(
            filters=3 * (num_classes + 5), kernel_size=1, activation=None,
        )
        self.decode93 = Decode(
            anchors=anchors[0],
            cell_width=8,
            num_classes=num_classes,
            xyscale=xyscales[0],
        )

        self.conv94 = YOLOConv2D(
            filters=256, kernel_size=3, strides=2, activation="leaky"
        )
        self.concat84_94 = layers.Concatenate(axis=-1)

        self.conv95 = YOLOConv2D(filters=256, kernel_size=1, activation="leaky")
        self.conv96 = YOLOConv2D(filters=512, kernel_size=3, activation="leaky")
        self.conv97 = YOLOConv2D(filters=256, kernel_size=1, activation="leaky")
        self.conv98 = YOLOConv2D(filters=512, kernel_size=3, activation="leaky")
        self.conv99 = YOLOConv2D(filters=256, kernel_size=1, activation="leaky")

        self.conv100 = YOLOConv2D(
            filters=512, kernel_size=3, activation="leaky"
        )
        self.conv101 = YOLOConv2D(
            filters=3 * (num_classes + 5), kernel_size=1, activation=None,
        )
        self.decode101 = Decode(
            anchors=anchors[1],
            cell_width=16,
            num_classes=num_classes,
            xyscale=xyscales[1],
        )

        self.conv102 = YOLOConv2D(
            filters=512, kernel_size=3, strides=2, activation="leaky"
        )
        self.concat77_102 = layers.Concatenate(axis=-1)

        self.conv103 = YOLOConv2D(
            filters=512, kernel_size=1, activation="leaky"
        )
        self.conv104 = YOLOConv2D(
            filters=1024, kernel_size=3, activation="leaky"
        )
        self.conv105 = YOLOConv2D(
            filters=512, kernel_size=1, activation="leaky"
        )
        self.conv106 = YOLOConv2D(
            filters=1024, kernel_size=3, activation="leaky"
        )
        self.conv107 = YOLOConv2D(
            filters=512, kernel_size=1, activation="leaky"
        )

        self.conv108 = YOLOConv2D(
            filters=1024, kernel_size=3, activation="leaky"
        )
        self.conv109 = YOLOConv2D(
            filters=3 * (num_classes + 5), kernel_size=1, activation=None,
        )
        self.decode109 = Decode(
            anchors=anchors[2],
            cell_width=32,
            num_classes=num_classes,
            xyscale=xyscales[2],
        )
        self.concat_total = layers.Concatenate(axis=1)

    def call(self, x, training: bool = False):
        route1, route2, route3 = self.csp_darknet53(x)

        x1 = self.conv78(route3)
        part2 = self.upSampling78(x1)
        part1 = self.conv79(route2)
        x1 = self.concat78_79([part1, part2])

        x1 = self.conv80(x1)
        x1 = self.conv81(x1)
        x1 = self.conv82(x1)
        x1 = self.conv83(x1)
        x1 = self.conv84(x1)

        x2 = self.conv85(x1)
        part2 = self.upSampling85(x2)
        part1 = self.conv86(route1)
        x2 = self.concat85_86([part1, part2])

        x2 = self.conv87(x2)
        x2 = self.conv88(x2)
        x2 = self.conv89(x2)
        x2 = self.conv90(x2)
        x2 = self.conv91(x2)

        s_bboxes = self.conv92(x2)
        s_bboxes = self.conv93(s_bboxes)
        # (batch, 3x, 3x, 3, (4 + 1 + num_classes))
        s_bboxes = self.decode93(s_bboxes, training)

        x2 = self.conv94(x2)
        x2 = self.concat84_94([x2, x1])

        x2 = self.conv95(x2)
        x2 = self.conv96(x2)
        x2 = self.conv97(x2)
        x2 = self.conv98(x2)
        x2 = self.conv99(x2)

        m_bboxes = self.conv100(x2)
        m_bboxes = self.conv101(m_bboxes)
        # (batch, 2x, 2x, 3, (4 + 1 + num_classes))
        m_bboxes = self.decode101(m_bboxes, training)

        x2 = self.conv102(x2)
        x2 = self.concat77_102([x2, route3])

        x2 = self.conv103(x2)
        x2 = self.conv104(x2)
        x2 = self.conv105(x2)
        x2 = self.conv106(x2)
        x2 = self.conv107(x2)

        l_bboxes = self.conv108(x2)
        l_bboxes = self.conv109(l_bboxes)
        # (batch, x, x, 3, (4 + 1 + num_classes))
        l_bboxes = self.decode109(l_bboxes, training)

        x = self.concat_total([s_bboxes, m_bboxes, l_bboxes])
        return x
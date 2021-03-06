"""
MIT License

Copyright (c) 2020-2021 Hyeonki Hong <hhk7734@gmail.com>

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
import platform

import numpy as np

try:
    import tflite_runtime.interpreter as tflite
    from tflite_runtime.interpreter import load_delegate
except ModuleNotFoundError:
    import tensorflow.lite as tflite

    load_delegate = tflite.experimental.load_delegate

from ..common.base_class import BaseClass
from ..common import yolo_layer as _yolo_layer

EDGETPU_SHARED_LIB = {
    "Linux": "libedgetpu.so.1",
    "Darwin": "libedgetpu.1.dylib",
    "Windows": "edgetpu.dll",
}[platform.system()]


class YOLOv4(BaseClass):
    def load_tflite(
        self, tflite_path: str, edgetpu_lib: str = EDGETPU_SHARED_LIB
    ) -> None:
        self._tpu = self.config.layer_count["yolo_tpu"] > 0

        if self._tpu:
            self._interpreter = tflite.Interpreter(
                model_path=tflite_path,
                experimental_delegates=[load_delegate(edgetpu_lib)],
            )
        else:
            self._interpreter = tflite.Interpreter(model_path=tflite_path)

        self._interpreter.allocate_tensors()

        # input_details
        input_details = self._interpreter.get_input_details()[0]
        if (
            input_details["shape"][1] != self.config.net.input_shape[0]
            or input_details["shape"][2] != self.config.net.input_shape[1]
            or input_details["shape"][3] != self.config.net.input_shape[2]
        ):
            raise RuntimeError(
                "YOLOv4: config.input_shape and tflite.input_details['shape']"
                " do not match."
            )
        self._input_details = input_details
        self._input_float = self._input_details["dtype"] is np.float32

        # output_details
        self._output_details = self._interpreter.get_output_details()

        layer_type = ""
        if self._tpu:
            layer_type = "yolo_tpu"
        else:
            layer_type = "yolo"

        self._anhcors = []
        self._scale_x_y = []
        self._beta_nms: float
        for i in range(self.config.layer_count[layer_type]):
            metayolo = self.config.find_metalayer(layer_type, i)
            self._beta_nms = metayolo.beta_nms
            self._anhcors.append(
                np.zeros((len(metayolo.mask), 2), dtype=np.float32)
            )
            self._scale_x_y.append(metayolo.scale_x_y)
            for j, n in enumerate(metayolo.mask):
                self._anhcors[i][j, 0] = (
                    metayolo.anchors[n][0] / self.config.net.width
                )
                self._anhcors[i][j, 1] = (
                    metayolo.anchors[n][1] / self.config.net.height
                )

    def summary(self):
        self.config.summary()

    #############
    # Inference #
    #############

    def _predict(self, x: np.ndarray) -> np.ndarray:
        self._interpreter.set_tensor(self._input_details["index"], x)
        self._interpreter.invoke()
        # [yolo0, yolo1, ...]
        # yolo == Dim(1, height, width, channels)
        # yolo_tpu == x, logistic(x)

        yolos = [
            self._interpreter.get_tensor(output_detail["index"])
            for output_detail in self._output_details
        ]

        candidates = []
        if self._tpu:
            num_yolo = len(yolos) // 2
            _yolos = []
            for i in range(num_yolo):
                _yolos.append(
                    _yolo_layer(
                        yolos[2 * i],
                        yolos[2 * i + 1],
                        self._anhcors[i],
                        self._scale_x_y[i],
                    )
                )

            stride = 5 + self.config.yolo_tpu_0.classes
            for yolo in _yolos:
                candidates.append(np.reshape(yolo, (1, -1, stride)))

        else:
            stride = 5 + self.config.yolo_0.classes
            for yolo in yolos:
                candidates.append(np.reshape(yolo, (1, -1, stride)))

        return np.concatenate(candidates, axis=1)

    def predict(self, frame: np.ndarray) -> np.ndarray:
        """
        Predict one frame

        @param frame: Dim(height, width, channels)

        @return pred_bboxes
            Dim(-1, (x,y,w,h,o, cls_id0, prob0, cls_id1, prob1))
        """
        # image_data == Dim(1, input_size[1], input_size[0], channels)
        height, width, _ = frame.shape

        image_data = self.resize_image(frame)
        if self._input_float:
            candidates = self._predict(
                image_data[np.newaxis, ...].astype(np.float32) / 255
            )[0]
        else:
            candidates = self._predict(image_data[np.newaxis, ...])[0]

        # Select 0
        pred_bboxes = self.yolo_diou_nms(candidates, self._beta_nms)
        self.fit_to_original(pred_bboxes, height, width)
        return pred_bboxes

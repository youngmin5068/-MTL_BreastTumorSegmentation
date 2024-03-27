import torch
import torch.nn as nn
import numpy as np
import model.tools as tools
import model.modules as m


class InterpreSegNet(nn.Module):
    def __init__(self, parameters):
        super(InterpreSegNet, self).__init__()


        self.experiment_parameters = parameters
        self.cam_size = parameters["cam_size"]

        self.global_network = m.GlobalNetwork(self.experiment_parameters, self)
        self.global_network.add_layers()

        # aggregation function
        self.aggregation_function = m.TopTPercentAggregationFunction(self.experiment_parameters, self)

        # detection module
        self.retrieve_roi_crops = m.RetrieveROIModule(self.experiment_parameters, self)

        # detection network
        self.local_network = m.LocalNetwork(self.experiment_parameters, self)
        self.local_network.add_layers()

        # MIL module
        self.attention_module = m.AttentionModule(self.experiment_parameters, self)
        self.attention_module.add_layers()

        # fusion branch
        self.fusion_dnn = nn.Linear(parameters["post_processing_dim"]+512, parameters["num_classes"])

    def _convert_crop_position(self, crops_x_small, cam_size, x_original):

        h, w = cam_size
        _, _, H, W = x_original.size()

        top_k_prop_x = crops_x_small[:, :, 0] / h
        top_k_prop_y = crops_x_small[:, :, 1] / w

        assert np.max(top_k_prop_x) <= 1.0, "top_k_prop_x >= 1.0"
        assert np.min(top_k_prop_x) >= 0.0, "top_k_prop_x <= 0.0"
        assert np.max(top_k_prop_y) <= 1.0, "top_k_prop_y >= 1.0"
        assert np.min(top_k_prop_y) >= 0.0, "top_k_prop_y <= 0.0"

        top_k_interpolate_x = np.expand_dims(np.around(top_k_prop_x * H), -1)
        top_k_interpolate_y = np.expand_dims(np.around(top_k_prop_y * W), -1)
        top_k_interpolate_2d = np.concatenate([top_k_interpolate_x, top_k_interpolate_y], axis=-1)
        return top_k_interpolate_2d

    def _retrieve_crop(self, x_original_pytorch, crop_positions, crop_method):

        batch_size, num_crops, _ = crop_positions.shape
        crop_h, crop_w = self.experiment_parameters["crop_shape"]

        output = torch.ones((batch_size, num_crops, crop_h, crop_w))
        if self.experiment_parameters["device_type"] == "gpu":
            device = torch.device("cuda:{}".format(self.experiment_parameters["gpu_number"]))
            output = output.to(device)
        for i in range(batch_size):
            for j in range(num_crops):
                tools.crop_pytorch(x_original_pytorch[i, 0, :, :],
                                                    self.experiment_parameters["crop_shape"],
                                                    crop_positions[i,j,:],
                                                    output[i,j,:,:],
                                                    method=crop_method)
        return output


    def forward(self, x_original):

        h_g, self.saliency_map, output = self.global_network.forward(x_original)

        self.y_global = self.aggregation_function.forward(self.saliency_map)
        small_x_locations = self.retrieve_roi_crops.forward(x_original, self.cam_size, self.saliency_map)
        self.patch_locations = self._convert_crop_position(small_x_locations, self.cam_size, x_original)

        crops_variable = self._retrieve_crop(x_original, self.patch_locations, self.retrieve_roi_crops.crop_method)

        self.patches = crops_variable.data.cpu().numpy()

        # detection network
        batch_size, num_crops, I, J = crops_variable.size()
        crops_variable = crops_variable.view(batch_size * num_crops, I, J).unsqueeze(1)
        h_crops = self.local_network.forward(crops_variable).view(batch_size, num_crops, -1)

        z, self.patch_attns, self.y_local = self.attention_module.forward(h_crops)

        g1, _ = torch.max(h_g, dim=2)
        global_vec, _ = torch.max(g1, dim=2)
        concat_vec = torch.cat([global_vec, z], dim=1)
        self.y_fusion = torch.sigmoid(self.fusion_dnn(concat_vec))


        return self.y_global, self.y_local, self.y_fusion, output
    




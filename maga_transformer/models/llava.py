import os
import time
import logging
import json
import functools
import torch
import re
import requests

from typing import List, Any, Dict, Tuple, Union
from PIL import Image
from io import BytesIO
from transformers.models.llama.tokenization_llama import LlamaTokenizer

from maga_transformer.config.gpt_init_model_parameters import GptInitModelParameters
from maga_transformer.models.llava_weight import LlavaWeightInfo, LlavaVitWeights
from maga_transformer.models.llama import Llama
from maga_transformer.models.base_model import BaseTokenizer, BaseModel
from maga_transformer.models.multimodal_mixin import MultiModalMixin
from maga_transformer.ops.comm.nccl_op import NcclOp
from maga_transformer.distribute.worker_info import g_parallel_info
from maga_transformer.models.llava_vit import LlavaImageEmbedding
from maga_transformer.utils.util import to_torch_dtype
from maga_transformer.model_factory_register import register_model

class LlavaTokenizer(BaseTokenizer):
    def __init__(self, 
                 tokenzier_path: str, 
                 mm_use_im_patch_token: bool,
                 mm_use_im_start_end: bool, 
                 image_expand: int, 
                 vit_special_token_ids: Dict[str, Any],
                 vit_special_tokens: Dict[str, Any]):
        self.tokenizer = LlamaTokenizer.from_pretrained(tokenzier_path)
        self.mm_use_im_patch_token = mm_use_im_patch_token
        self.mm_use_im_start_end = mm_use_im_start_end

        extra_tokens: List[str] = []
        if self.mm_use_im_patch_token:
            extra_tokens.extend(["<im_patch>"])
        if self.mm_use_im_start_end:
            extra_tokens.extend(["<im_start>", "<im_end>"])
        self.tokenizer.add_tokens(extra_tokens, special_tokens=True)

        self.image_expand = image_expand
        self.image_token_index: int = vit_special_token_ids["image_token_index"]
        self.ignore_token_index: int = vit_special_token_ids["ignore_token_index"]
        self.default_image_token = vit_special_tokens["default_image_token"]
        self.default_im_start_token = vit_special_tokens["default_im_start_token"]
        self.default_im_end_token = vit_special_tokens["default_im_end_token"]
        self.bos_id = self.tokenizer.sp_model.bos_id()
        

    def encode(self, s: str) -> List[int]:
        replace_token = self.default_image_token
        if self.mm_use_im_start_end:
            replace_token = self.default_im_start_token + replace_token + self.default_im_end_token
        s = s.replace(self.default_image_token, replace_token)
        
        prompt_chunks: List[List[int]] = [self.tokenizer.encode(chunk) for chunk in s.split(self.default_image_token)]

        images = len(prompt_chunks) - 1
        
        def insert_separator(X, sep):
            return [ele for sublist in zip(X, [sep]*len(X)) for ele in sublist][:-1]

        t: List[int] = []
        offset = 0
        if len(prompt_chunks) > 0 and len(prompt_chunks[0]) > 0 and prompt_chunks[0][0] == self.bos_id:
            offset = 1
            t.append(prompt_chunks[0][0])

        for x in insert_separator(prompt_chunks, [self.image_token_index] * (offset + 1)):
            t.extend(x[offset:])

        t.extend([self.ignore_token_index] * images * (self.image_expand - 1))

        return t

    def decode(self, t: List[int]) -> str:
        return self.tokenizer.decode(t)

class Llava(Llama, MultiModalMixin):
    def __init__(self, config: GptInitModelParameters):
        self.visual = LlavaImageEmbedding(config.vit_related_params)
        self.nccl_op_ = NcclOp()
        config.vit_related_params["proj_layers"] = self.visual.proj_layers
        config.vit_related_params["weights"] = LlavaVitWeights(config.vit_related_params)
        Llama.__init__(self, config)
    
    @staticmethod
    def multimodal_modify_prompt_plugin(prompt: Union[List[Dict[str, Any]], str], images: List[str], 
                                        img_token: str, **kwargs: Any) -> Tuple[str, List[Any]]:
        prompt, images = MultiModalMixin.multimodal_modify_prompt_plugin(prompt, images, img_token, **kwargs)
        if img_token in prompt:
            return prompt, images
        else:
            return prompt + (img_token + "\n") * len(images), images

    @staticmethod
    def _create_config(ckpt_path):
        config = GptInitModelParameters(
            head_num=0,
            size_per_head=0,
            layer_num=0,
            max_seq_len=0,
            vocab_size=0,
            ckpt_path=ckpt_path,
            activation_type="SiGLU",
            norm_type="rmsnorm",
            rotary_embedding_dim=128,
            rotary_embedding_style=1,
            has_post_decoder_layernorm=True,
            is_multimodal=True
        )
        # hugggingface
        config_path = os.path.join(ckpt_path, "config.json")
        param_path = os.path.join(ckpt_path, "params.json")
        if os.path.exists(config_path):
            with open(config_path) as reader:
                content = reader.read()
                content = content.replace("LlavaForCausalLM", "LLaVAForCausalLM")
                config_json = json.loads(content)
            Llava.from_huggingface(config, config_json)
        else:
            raise Exception("llava parameter from unkown source")
        config.tp_split_emb_and_lm_head = False # llava embedding can't tp
        return config

    @staticmethod
    def get_weight_cls():
        return LlavaWeightInfo
   
    @staticmethod
    def from_huggingface(config: GptInitModelParameters, config_json: Dict[str, Any]):
        Llama.from_huggingface(config, config_json)

        vit_related_params_list = [
            ("mm_use_im_patch_token", False),
            ("mm_use_im_start_end", False),
            ("image_aspect_ratio", None),
            ("tune_mm_mlp_adapter", False),
            ("mm_projector_type", "linear"),
            ("hidden_size", 0),
            ("mm_vision_select_layer", None),
            ("mm_vision_select_feature", "patch")
        ]

        for param_name, default_value in vit_related_params_list:
            config.vit_related_params[param_name] = config_json.get(param_name, default_value)

        config.vit_related_params["mm_hidden_size"] = config_json.get("mm_hidden_size", config_json["hidden_size"])
        config.vit_related_params["vit_layer_id_interval"] = 2
        config.vit_related_params["num_hidden_layers"] = 12
        config.vit_related_params["vit_special_token_ids"].update({"ignore_token_index": -100, "image_token_index": -200})
        config.vit_related_params["vit_special_tokens"].update({
            "default_image_token": "<image>", 
            "default_im_start_token": "<im_start>", 
            "default_im_end_token": "<im_end>"
        })

        vis_tower_name = config_json.get("mm_vision_tower", config_json.get("vision_tower", None))
        img_expand_match = re.search("patch(\d+)-(\d+)", vis_tower_name)
        if img_expand_match:
            patch_size = int(img_expand_match.group(1))
            img_size = int(img_expand_match.group(2))
            config.vit_related_params["patch_size"] = patch_size
            config.vit_related_params["image_size"] = img_size
            config.vit_related_params["img_expand_len"] = (img_size // patch_size) ** 2
        config.vit_related_params["vit_tower_path"] = vis_tower_name

    def load_tokenizer(self):
        self.tokenizer = LlavaTokenizer(self.config.tokenizer_path, 
                                        self.config.vit_related_params["mm_use_im_patch_token"], 
                                        self.config.vit_related_params["mm_use_im_start_end"], 
                                        self.config.vit_related_params["img_expand_len"], 
                                        self.config.vit_related_params["vit_special_token_ids"],
                                        self.config.vit_related_params["vit_special_tokens"])

    def encode_images(self, images):
        if images.shape[0] == 0:
            return images
        image_features = self.vision_tower(images).to(device=self.device)
        image_features = self.mm_projector(image_features)
        return image_features
    
    def async_input_word_embedding(self, inputs: torch.Tensor, images: List[List[str]]):
        inputs = inputs.reshape(1, -1)
        if g_parallel_info.tp_size <= 1:
            return self.multimodal_embedding(inputs, images).squeeze(0)

        if g_parallel_info.tp_rank == 0:
            embedding_tensor = self.multimodal_embedding(inputs, images).squeeze(0)
        else:
            embedding_tensor = torch.zeros((inputs.shape[1], self.config.head_num * self.config.size_per_head), dtype=torch.float16, device="cuda:0")
        self.nccl_op_.broadcast_tp([embedding_tensor])
        return embedding_tensor
        
    def input_word_embedding(self, inputs: torch.Tensor, images: List[List[str]]):
        return self.multimodal_embedding(inputs, images)

    def multimodal_embedding(
        self, input_ids: torch.Tensor, images: List[List[str]]
    ):
        image_token_index = self.config.vit_related_params["vit_special_token_ids"]["image_token_index"]
        ignore_token_index = self.config.vit_related_params["vit_special_token_ids"]["ignore_token_index"]

        assert isinstance(images, list) and isinstance(images[0], list)

        for i in range(input_ids.shape[0]):
            if (input_ids[i] == image_token_index).sum() != len(images[i]):
                raise ValueError("Number of images does not match number of <image> tokens in prompt")

        image_features = self.visual.image_embedding(images, self.device)

        new_input_embeds = []

        tune_mm_mlp_adapter = self.config.vit_related_params["tune_mm_mlp_adapter"]
        mm_use_im_start_end = self.config.vit_related_params["mm_use_im_start_end"]
        append_extra_tokens = tune_mm_mlp_adapter and mm_use_im_start_end

        for batch_idx, cur_input_ids in enumerate(input_ids):
            cur_input_ids = cur_input_ids[~(cur_input_ids == ignore_token_index)]
            image_token_indices = torch.where(cur_input_ids == image_token_index)[0]
            cur_new_input_embeds = []
            cur_image_idx = 0
            if len(image_features[batch_idx]) == 0:
                cur_new_input_embeds = self.word_embedding(cur_input_ids)
            else:
                while image_token_indices.numel() > 0:
                    cur_image_features = image_features[batch_idx][cur_image_idx]
                    image_token_start = image_token_indices[0]
                    if append_extra_tokens:
                        cur_new_input_embeds.append(self.word_embedding(cur_input_ids[:image_token_start-1]).detach())
                        cur_new_input_embeds.append(self.word_embedding(cur_input_ids[image_token_start-1:image_token_start]))
                        cur_new_input_embeds.append(cur_image_features)
                        cur_new_input_embeds.append(self.word_embedding(cur_input_ids[image_token_start+1:image_token_start+2]))
                    else:
                        cur_new_input_embeds.append(self.word_embedding(cur_input_ids[:image_token_start]))
                        cur_new_input_embeds.append(cur_image_features)
                    
                    cur_image_idx += 1
                    if append_extra_tokens:
                        cur_input_ids = cur_input_ids[image_token_start+2:]
                    else:
                        cur_input_ids = cur_input_ids[image_token_start+1:]
                    image_token_indices = torch.where(cur_input_ids == image_token_index)[0]
                
                if cur_input_ids.numel() > 0:
                    if append_extra_tokens:
                        cur_new_input_embeds.append(self.word_embedding(cur_input_ids).detach())
                    else:
                        cur_new_input_embeds.append(self.word_embedding(cur_input_ids))

                cur_new_input_embeds = [x.to(device=self.device) for x in cur_new_input_embeds]
                cur_new_input_embeds = torch.cat(cur_new_input_embeds, dim=0)
            new_input_embeds.append(cur_new_input_embeds)

        if any(x.shape != new_input_embeds[0].shape for x in new_input_embeds):
            max_len = max(x.shape[0] for x in new_input_embeds)
            max_input_len = max(x.shape[0] for x in new_input_ids)

            new_input_embeds_align = []
            for cur_new_embed in new_input_embeds:
                cur_new_embed = torch.cat((cur_new_embed, torch.zeros((max_len - cur_new_embed.shape[0], cur_new_embed.shape[1]), dtype=cur_new_embed.dtype, device=cur_new_embed.device)), dim=0)
                new_input_embeds_align.append(cur_new_embed)
            new_input_embeds = torch.stack(new_input_embeds_align, dim=0)
        else:
            new_input_embeds = torch.stack(new_input_embeds, dim=0)

        assert input_ids.shape[1] == new_input_embeds.shape[1]
        return new_input_embeds.type(to_torch_dtype(self.config.data_type))

    @staticmethod
    def eval_model_size(config: GptInitModelParameters):
        llm_size = BaseModel.eval_model_size(config)
        vision_config_dict = config.vit_related_params

        hidden_size = vision_config_dict["hidden_size"]
        patch_num = vision_config_dict["image_size"] // vision_config_dict["patch_size"]
        conv_size = patch_num ** 2 * hidden_size * 3
        pos_emb_size = patch_num ** 2 * hidden_size
        ln_size = 2 * hidden_size * 2

        clip_encoder_size = vision_config_dict["num_hidden_layers"] * (hidden_size ** 2 * 4 + hidden_size * 2 * 2 + hidden_size * vision_config_dict["intermediate_size"] * 2)

        data_type = vision_config_dict["torch_dtype"]
        if data_type == "float32":
            data_type_size = 4
        elif data_type == "int8":
            data_type_size = 1
        else:
            data_type_size = 2
        llm_size += (conv_size + pos_emb_size + ln_size + clip_encoder_size) * data_type_size

        return llm_size
    
register_model("llava", Llava)

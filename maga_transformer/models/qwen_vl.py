
import torch
import os
import json
from typing import List, Any, Tuple, Dict

from transformers import AutoTokenizer

from maga_transformer.config.gpt_init_model_parameters import GptInitModelParameters
from maga_transformer.models.qwen import QWen
from maga_transformer.models.qwen_vl_weight import QWenVLWeightInfo, QwenVLVitWeight
from maga_transformer.models.qwen_vl_vit import VisionTransformer as QWen_VL_ViT
from maga_transformer.models.base_model import BaseModel
from maga_transformer.models.multimodal_mixin import MultiModalMixin, BaseImageEmbedding
from maga_transformer.model_factory_register import register_model

class QwenVLImageEmbedding(BaseImageEmbedding):
    def __init__(self, config: Dict[str, Any]):
        self.vit = QWen_VL_ViT(**config)
        self.weights = QwenVLVitWeight(self.vit)
    
    def image_embedding(self, images: List[str], device) -> torch.Tensor:
        if len(images) != 0:
            images = self.vit.encode(images)
            assert images.shape[0] == len(images)
        return images.to(device=device)

class QWen_VL(QWen, MultiModalMixin):
    def __init__(self, config: GptInitModelParameters):
        self.visual = QwenVLImageEmbedding(config.vit_related_params)
        config.vit_related_params["weights"] = self.visual.weights

        QWen.__init__(self, config)

    @staticmethod
    def multimodal_modify_prompt_plugin(prompt: str, **kwargs: Any) -> Tuple[str, List[Any]]:
        prompt, images = MultiModalMixin.multimodal_modify_prompt_plugin(prompt, **kwargs)
        img_token: str = kwargs.get('img_token')
        if img_token in prompt:
            split_prompts = prompt.split(img_token)
            if len(split_prompts) - 1 != len(images):
                raise Exception('num of ' + img_token + ' should equals to images num')
            res = split_prompts[0]
            idx = 0
            for split_prompt in split_prompts[1:]:
                res = res + '<img>' + images[idx] + '</img>' + split_prompt
                idx = idx + 1
            return res, images
        else:
            prefix_prompt = ''
            if len(images) > 0:
                for i in range(len(images)):
                    prefix_prompt += 'Picture {i}:<img>'.format(i = i + 1) + images[i] + '</img>\n'
            return prefix_prompt + prompt, images
    
    @staticmethod
    def _create_config(ckpt_path: str):
        config = GptInitModelParameters(
            head_num=0,
            size_per_head=0,
            layer_num=0,
            max_seq_len=0,
            vocab_size=0,
            is_multimodal=True
        )
        QWen_VL._common_config(config, ckpt_path)
        return config

    @staticmethod
    def _common_config(config: GptInitModelParameters, ckpt_path: str) -> GptInitModelParameters:
        QWen._common_config(config, ckpt_path)
        QWen._from_hf(config, ckpt_path)
        QWen_VL._load_vit_param(config, ckpt_path)
        return config
    
    @staticmethod
    def _load_vit_param(config: GptInitModelParameters, ckpt_path: str):
        config_path = os.path.join(ckpt_path, "config.json")
        if not os.path.exists(config_path):
            return
        with open(config_path) as reader:
            content = reader.read()
            config_json = json.loads(content)

        vit_config = config_json['visual']
        config.vit_related_params.update(vit_config)
        config.vit_related_params["vit_special_token_ids"].update({
            'image_start_id': vit_config['image_start_id'],
            'image_end_id': vit_config['image_start_id'] + 1,
            'image_pad_id': vit_config['image_start_id'] + 2})
        config.vit_related_params["vit_special_tokens"].update({'default_image_token': '<img/>'})
    
    def load_tokenizer(self):
        self.tokenizer = AutoTokenizer.from_pretrained(self.config.tokenizer_path, trust_remote_code=True)

    @staticmethod
    def get_weight_cls():
        return QWenVLWeightInfo

    def async_input_word_embedding(self, inputs: torch.Tensor, images: List[List[str]]):
        inputs = inputs.reshape(1, -1)
        return self.multimodal_embedding(inputs).squeeze(0)

    def input_word_embedding(self, inputs: torch.Tensor, images: List[List[str]]):
        return self.multimodal_embedding(inputs)
    
    # QWen_VL tokenizer encode image urls into tokens, so that multimodal_embedding don't need images as input
    def multimodal_embedding(self, input_ids: torch.Tensor):
        img_start_id: int = self.config.vit_related_params['vit_special_token_ids']['image_start_id']
        img_end_id: int = self.config.vit_related_params['vit_special_token_ids']['image_end_id']
        img_pad_id: int = self.config.vit_related_params['vit_special_token_ids']['image_pad_id']
        bos_pos = torch.where(input_ids == img_start_id)
        eos_pos = torch.where(input_ids == img_end_id)
        assert (bos_pos[0] == eos_pos[0]).all()
        img_pos = torch.stack((bos_pos[0], bos_pos[1], eos_pos[1]), dim=1)
        images = []
        for i, a, b in img_pos:
            image = input_ids[i][a + 1 : b].tolist()
            if img_pad_id in image:
                image = image[ : image.index(img_pad_id)]
            images.append(bytes(image).decode('utf-8'))

        if len(images) != 0:
            images = self.visual.image_embedding(images, self.device)
            assert images.shape[0] == len(images)

        input_embeds = self.word_embedding(input_ids)

        if images != []:
            for idx, (i, a, b) in enumerate(img_pos):
                input_embeds[i][a + 1: b] = images[idx]

        return input_embeds

    @staticmethod
    def eval_model_size(config: GptInitModelParameters):
        llm_size = BaseModel.eval_model_size(config)
        
        embed_dim = config.vit_related_params["output_dim"]
        width = config.vit_related_params["width"]
        layers = config.vit_related_params["layers"]
        patch_size = config.vit_related_params["patch_size"]
        mlp_ratio = config.vit_related_params["mlp_ratio"]
        mlp_width = int(mlp_ratio * width)
        data_width = 4
        
        llm_size += (3 * width * patch_size ** 2 + width * 2) * data_width
        llm_size += (layers * (width * 2 * 2 + width ** 2 * 4 + width * 4 + mlp_width * width * 2 + mlp_width + width)) * data_width
        llm_size += (width * embed_dim + embed_dim ** 2 + embed_dim + embed_dim * 2 * 3) * data_width

        return llm_size
    
register_model('qwen_vl', QWen_VL)
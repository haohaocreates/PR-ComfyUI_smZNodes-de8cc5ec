import torch
from . import devices
from . import prompt_parser
from . import shared
from comfy import model_management
def catenate_conds(conds):
    if not isinstance(conds[0], dict):
        return torch.cat(conds)

    return {key: torch.cat([x[key] for x in conds]) for key in conds[0].keys()}


def subscript_cond(cond, a, b):
    if not isinstance(cond, dict):
        return cond[a:b]

    return {key: vec[a:b] for key, vec in cond.items()}


def pad_cond(tensor, repeats, empty):
    if not isinstance(tensor, dict):
        return torch.cat([tensor, empty.repeat((tensor.shape[0], repeats, 1)).to(device=tensor.device)], axis=1)

    tensor['crossattn'] = pad_cond(tensor['crossattn'], repeats, empty)
    return tensor


class CFGDenoiser(torch.nn.Module):
    """
    Classifier free guidance denoiser. A wrapper for stable diffusion model (specifically for unet)
    that can take a noisy picture and produce a noise-free picture using two guidances (prompts)
    instead of one. Originally, the second prompt is just an empty string, but we use non-empty
    negative prompt.
    """

    def __init__(self, model):
        super().__init__()
        self.inner_model = model
        self.model_wrap = None
        self.mask = None
        self.nmask = None
        self.init_latent = None
        self.steps = None
        """number of steps as specified by user in UI"""

        self.total_steps = None
        """expected number of calls to denoiser calculated from self.steps and specifics of the selected sampler"""

        self.step = 0
        self.image_cfg_scale = None
        self.padded_cond_uncond = False
        self.sampler = None
        self.model_wrap = None
        self.p = None
        self.mask_before_denoising = False
        import comfy
        import inspect
        apply_model_src = inspect.getsource(comfy.model_base.BaseModel.apply_model_orig)
        self.c_crossattn_as_list =  'torch.cat(c_crossattn, 1)' in apply_model_src

    # @property
    # def inner_model(self):
    #     raise NotImplementedError()

    def combine_denoised(self, x_out, conds_list, uncond, cond_scale):
        denoised_uncond = x_out[-uncond.shape[0]:]
        denoised = torch.clone(denoised_uncond)

        for i, conds in enumerate(conds_list):
            for cond_index, weight in conds:
                denoised[i] += (x_out[cond_index] - denoised_uncond[i]) * (weight * cond_scale)

        return denoised

    def combine_denoised_for_edit_model(self, x_out, cond_scale):
        out_cond, out_img_cond, out_uncond = x_out.chunk(3)
        denoised = out_uncond + cond_scale * (out_cond - out_img_cond) + self.image_cfg_scale * (out_img_cond - out_uncond)

        return denoised

    def get_pred_x0(self, x_in, x_out, sigma):
        return x_out

    def update_inner_model(self):
        self.model_wrap = None

        c, uc = self.p.get_conds()
        self.sampler.sampler_extra_args['cond'] = c
        self.sampler.sampler_extra_args['uncond'] = uc
    
    def make_condition_dict(self, x, d):
        if x.c_adm is not None:
            k = x.c_adm['key']
            d[k] = x.c_adm[k]
        d['c_crossattn'] = d['c_crossattn'].to(device=x.device)
        return d

    def forward(self, x, sigma, uncond, cond, cond_scale, s_min_uncond, image_cond):
        model_management.throw_exception_if_processing_interrupted()
        # if state.interrupted or state.skipped:
        #     raise sd_samplers_common.InterruptedException

        # if sd_samplers_common.apply_refiner(self):
        #     cond = self.sampler.sampler_extra_args['cond']
        #     uncond = self.sampler.sampler_extra_args['uncond']

        # at self.image_cfg_scale == 1.0 produced results for edit model are the same as with normal sampling,
        # so is_edit_model is set to False to support AND composition.
        # is_edit_model = shared.sd_model.cond_stage_key == "edit" and self.image_cfg_scale is not None and self.image_cfg_scale != 1.0
        is_edit_model = False

        conds_list, tensor = cond
        assert not is_edit_model or all(len(conds) == 1 for conds in conds_list), "AND is not supported for InstructPix2Pix checkpoint (unless using Image CFG scale = 1.0)"

        if self.mask_before_denoising and self.mask is not None:
            x = self.init_latent * self.mask + self.nmask * x

        batch_size = len(conds_list)
        repeats = [len(conds_list[i]) for i in range(batch_size)]
        if not hasattr(x, 'c_adm'):
            x.c_adm = None

        # if shared.sd_model.model.conditioning_key == "crossattn-adm":
        #     image_uncond = torch.zeros_like(image_cond)
        #     make_condition_dict = lambda c_crossattn: {"c_crossattn": c_crossattn} # pylint: disable=C3001
        # else:
        #     image_uncond = image_cond
        #     if isinstance(uncond, dict):
        #         make_condition_dict = lambda c_crossattn, c_concat: {**c_crossattn, "c_concat": [c_concat]}
        #     else:
        #         make_condition_dict = lambda c_crossattn, c_concat: {"c_crossattn": [c_crossattn], "c_concat": [c_concat]}

        # unclip
        # if shared.sd_model.model.conditioning_key == "crossattn-adm":
        if False:
            image_uncond = torch.zeros_like(image_cond)
            if self.c_crossattn_as_list:
                make_condition_dict = lambda c_crossattn: {"c_crossattn": [ctn.to(device=self.device) for ctn in c_crossattn] if type(c_crossattn) is list else [c_crossattn.to(device=self.device)], 'transformer_options': {'from_smZ': True}} # pylint: disable=C3001
            else:
                make_condition_dict = lambda c_crossattn: {"c_crossattn": c_crossattn, 'transformer_options': {'from_smZ': True}} # pylint: disable=C3001
        else:
            image_uncond = image_cond
            if isinstance(uncond, dict):
                make_condition_dict = lambda c_crossattn, c_concat: {**c_crossattn, "c_concat": None, 'transformer_options': {'from_smZ': True}}
            else:
                if self.c_crossattn_as_list:
                    make_condition_dict = lambda c_crossattn, c_concat: {"c_crossattn": c_crossattn if type(c_crossattn) is list else [c_crossattn], "c_concat": None, 'transformer_options': {'from_smZ': True}}
                else:
                    make_condition_dict = lambda c_crossattn, c_concat: {"c_crossattn": c_crossattn, "c_concat": None, 'transformer_options': {'from_smZ': True}}
        
        _make_condition_dict = make_condition_dict
        make_condition_dict = lambda *a, **kwa: self.make_condition_dict(x, _make_condition_dict(*a, **kwa))

        if not is_edit_model:
            x_in = torch.cat([torch.stack([x[i] for _ in range(n)]) for i, n in enumerate(repeats)] + [x])
            sigma_in = torch.cat([torch.stack([sigma[i] for _ in range(n)]) for i, n in enumerate(repeats)] + [sigma])
            image_cond_in = torch.cat([torch.stack([image_cond[i] for _ in range(n)]) for i, n in enumerate(repeats)] + [image_uncond])
        else:
            x_in = torch.cat([torch.stack([x[i] for _ in range(n)]) for i, n in enumerate(repeats)] + [x] + [x])
            sigma_in = torch.cat([torch.stack([sigma[i] for _ in range(n)]) for i, n in enumerate(repeats)] + [sigma] + [sigma])
            image_cond_in = torch.cat([torch.stack([image_cond[i] for _ in range(n)]) for i, n in enumerate(repeats)] + [image_uncond] + [torch.zeros_like(self.init_latent)])

        # denoiser_params = CFGDenoiserParams(x_in, image_cond_in, sigma_in, state.sampling_step, state.sampling_steps, tensor, uncond)
        # cfg_denoiser_callback(denoiser_params)
        # x_in = denoiser_params.x
        # image_cond_in = denoiser_params.image_cond
        # sigma_in = denoiser_params.sigma
        # tensor = denoiser_params.text_cond
        # uncond = denoiser_params.text_uncond
        skip_uncond = False

        # alternating uncond allows for higher thresholds without the quality loss normally expected from raising it
        if self.step % 2 and s_min_uncond > 0 and sigma[0] < s_min_uncond and not is_edit_model:
            skip_uncond = True
            x_in = x_in[:-batch_size]
            sigma_in = sigma_in[:-batch_size]

        self.padded_cond_uncond = False
        if shared.opts.pad_cond_uncond and tensor.shape[1] != uncond.shape[1]:
            empty = shared.sd_model.cond_stage_model_empty_prompt
            num_repeats = (tensor.shape[1] - uncond.shape[1]) // empty.shape[1]

            if num_repeats < 0:
                tensor = pad_cond(tensor, -num_repeats, empty)
                self.padded_cond_uncond = True
            elif num_repeats > 0:
                uncond = pad_cond(uncond, num_repeats, empty)
                self.padded_cond_uncond = True

        if tensor.shape[1] == uncond.shape[1] or skip_uncond:
            if is_edit_model:
                cond_in = catenate_conds([tensor, uncond, uncond])
            elif skip_uncond:
                cond_in = tensor
            else:
                cond_in = catenate_conds([tensor, uncond])

            if shared.opts.batch_cond_uncond:
                x_out = self.inner_model(x_in, sigma_in, **make_condition_dict(cond_in, image_cond_in))
            else:
                x_out = torch.zeros_like(x_in)
                for batch_offset in range(0, x_out.shape[0], batch_size):
                    a = batch_offset
                    b = a + batch_size
                    x_out[a:b] = self.inner_model(x_in[a:b], sigma_in[a:b], **make_condition_dict(subscript_cond(cond_in, a, b), image_cond_in[a:b]))
        else:
            x_out = torch.zeros_like(x_in)
            batch_size = batch_size*2 if shared.opts.batch_cond_uncond else batch_size
            for batch_offset in range(0, tensor.shape[0], batch_size):
                a = batch_offset
                b = min(a + batch_size, tensor.shape[0])

                if not is_edit_model:
                    c_crossattn = subscript_cond(tensor, a, b)
                else:
                    c_crossattn = torch.cat([tensor[a:b]], uncond)

                x_out[a:b] = self.inner_model(x_in[a:b], sigma_in[a:b], **make_condition_dict(c_crossattn, image_cond_in[a:b]))

            if not skip_uncond:
                x_out[-uncond.shape[0]:] = self.inner_model(x_in[-uncond.shape[0]:], sigma_in[-uncond.shape[0]:], **make_condition_dict(uncond, image_cond_in[-uncond.shape[0]:]))

        denoised_image_indexes = [x[0][0] for x in conds_list]
        if skip_uncond:
            fake_uncond = torch.cat([x_out[i:i+1] for i in denoised_image_indexes])
            x_out = torch.cat([x_out, fake_uncond])  # we skipped uncond denoising, so we put cond-denoised image to where the uncond-denoised image should be

        # denoised_params = CFGDenoisedParams(x_out, state.sampling_step, state.sampling_steps, self.inner_model)
        # cfg_denoised_callback(denoised_params)

        devices.test_for_nans(x_out, "unet")

        if is_edit_model:
            denoised = self.combine_denoised_for_edit_model(x_out, cond_scale)
        elif skip_uncond:
            denoised = self.combine_denoised(x_out, conds_list, uncond, 1.0)
        else:
            denoised = self.combine_denoised(x_out, conds_list, uncond, cond_scale)

        if not self.mask_before_denoising and self.mask is not None:
            denoised = self.init_latent * self.mask + self.nmask * denoised

        # self.sampler.last_latent = self.get_pred_x0(torch.cat([x_in[i:i + 1] for i in denoised_image_indexes]), torch.cat([x_out[i:i + 1] for i in denoised_image_indexes]), sigma)

        # if opts.live_preview_content == "Prompt":
        #     preview = self.sampler.last_latent
        # elif opts.live_preview_content == "Negative prompt":
        #     preview = self.get_pred_x0(x_in[-uncond.shape[0]:], x_out[-uncond.shape[0]:], sigma)
        # else:
        #     preview = self.get_pred_x0(torch.cat([x_in[i:i+1] for i in denoised_image_indexes]), torch.cat([denoised[i:i+1] for i in denoised_image_indexes]), sigma)

        # sd_samplers_common.store_latent(preview)

        # after_cfg_callback_params = AfterCFGCallbackParams(denoised, state.sampling_step, state.sampling_steps)
        # cfg_after_cfg_callback(after_cfg_callback_params)
        # denoised = after_cfg_callback_params.x

        self.step += 1
        del x_out
        return denoised
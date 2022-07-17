import os
import jittor as jt
from PIL import Image
import numpy as np
from tqdm import tqdm
from jnerf.ops.code_ops import *
from jnerf.dataset.dataset import jt_srgb_to_linear, jt_linear_to_srgb
from jnerf.utils.config import get_cfg, save_cfg
from jnerf.utils.registry import build_from_cfg,NETWORKS,SCHEDULERS,DATASETS,OPTIMS,SAMPLERS,LOSSES
from jnerf.models.losses.mse_loss import img2mse, mse2psnr
from jnerf.dataset import camera_path
import cv2
from jnerf.utils.config import init_cfg



class Runner():
    def __init__(self):
        self.cfg = get_cfg()
        if self.cfg.fp16 and jt.flags.cuda_archs[0] < 70:
            print("Warning: Sm arch is lower than sm_70, fp16 is not supported. Automatically use fp32 instead.")
            self.cfg.fp16 = False
        if not os.path.exists(self.cfg.log_dir):
            os.makedirs(self.cfg.log_dir)
        self.exp_name           = self.cfg.exp_name
        self.dataset            = {}
        self.dataset["train"]   = build_from_cfg(self.cfg.dataset.test, DATASETS)
        # self.dataset["train"] = None
        self.cfg.dataset_obj    = self.dataset["train"]
        if self.cfg.dataset.val:
            self.dataset["val"] = build_from_cfg(self.cfg.dataset.test, DATASETS)
          #  self.dataset["val"] = None
        else:
            self.dataset["val"] = self.dataset["train"]
        self.dataset["test"]    = None
        self.model              = build_from_cfg(self.cfg.model, NETWORKS)
        self.cfg.model_obj      = self.model
        self.sampler            = build_from_cfg(self.cfg.sampler, SAMPLERS)
        self.cfg.sampler_obj    = self.sampler
        self.optimizer          = build_from_cfg(self.cfg.optim, OPTIMS, params=self.model.parameters())
        self.optimizer          = build_from_cfg(self.cfg.expdecay, OPTIMS, nested_optimizer=self.optimizer)
        self.ema_optimizer      = build_from_cfg(self.cfg.ema, OPTIMS, params=self.model.parameters())
        self.loss_func          = build_from_cfg(self.cfg.loss, LOSSES)
        self.background_color   = self.cfg.background_color
        self.tot_train_steps    = self.cfg.tot_train_steps
        self.n_rays_per_batch   = self.cfg.n_rays_per_batch
        self.using_fp16         = self.cfg.fp16
        self.save_path          = os.path.join(self.cfg.log_dir, self.exp_name)

        if not os.path.exists(self.save_path):
            os.makedirs(self.save_path)
        if self.cfg.ckpt_path and self.cfg.ckpt_path is not None:
            self.ckpt_path = self.cfg.ckpt_path
        else:
            self.ckpt_path = os.path.join(self.save_path, "params.pkl")
        if self.cfg.load_ckpt:
            self.load_ckpt(self.ckpt_path)
        else:
            self.start=0

        self.cfg.m_training_step = 0
        self.val_freq = 4096
        self.image_resolutions = self.dataset["train"].resolution
        self.W = self.image_resolutions[0]
        self.H = self.image_resolutions[1]

    def test(self, load_ckpt=False):
        if load_ckpt:
            assert os.path.exists(self.ckpt_path), "ckpt file does not exist: "+self.ckpt_path
            self.load_ckpt(self.ckpt_path)
        if self.dataset["test"] is None:
            self.dataset["test"] = build_from_cfg(self.cfg.dataset.test, DATASETS)
        if not os.path.exists(os.path.join(self.save_path, "test")):
            os.makedirs(os.path.join(self.save_path, "test"))
        mse_list=self.render_test(save_path=os.path.join(self.save_path, "test"))
        if self.dataset["test"].have_img:
            tot_psnr=0
            for mse in mse_list:
                tot_psnr += mse2psnr(mse)
            print("TOTAL TEST PSNR===={}".format(tot_psnr/len(mse_list)))

    def load_ckpt(self, path):
        print("Loading ckpt from:", path)
        ckpt = jt.load(path)
        self.start = ckpt['global_step']
        self.model.load_state_dict(ckpt['model'])
        if self.using_fp16:
            self.model.set_fp16()
        self.sampler.load_state_dict(ckpt['sampler'])
        self.optimizer.load_state_dict(ckpt['optimizer'])
        nested = ckpt['nested_optimizer']['defaults']['param_groups'][0]
        for pg in self.optimizer._nested_optimizer.param_groups:
            for i in range(len(pg["params"])):
                pg["values"][i] = jt.array(nested["values"][i])
                pg["m"][i] = jt.array(nested["m"][i])
        ema = ckpt['ema_optimizer']['defaults']['param_groups'][0]
        for pg in self.ema_optimizer.param_groups:
            for i in range(len(pg["params"])):
                pg["values"][i] = jt.array(ema["values"][i])
        self.ema_optimizer.steps = ckpt['ema_optimizer']['defaults']['steps']



    def render_test(self, save_img=True, save_path=None):
        # save_path = "result"
        if save_path is None:
            save_path = self.save_path
        mse_list = []
        print("rendering testset...")
        for img_i in tqdm(range(0, self.dataset["test"].n_images, 1)):
        # for img_i in tqdm(range(0, 10, 1)):
            with jt.no_grad():
                imgs = []
                for i in range(1):
                    simg, img_tar = self.render_img(dataset_mode="test", img_id=img_i)
                    imgs.append(simg)
                img = np.stack(imgs, axis=0).mean(0)
                if save_img:
                    self.save_img(save_path + f"/{self.exp_name}_r_{img_i}.png", img)
                    if self.dataset["test"].have_img:
                        self.save_img(save_path + f"/{self.exp_name}_gt_{img_i}.png", img_tar)
                mse_list.append(img2mse(
                    jt.array(img),
                    jt.array(img_tar)).item())
        return mse_list

    def save_img(self, path, img):

        if isinstance(img, np.ndarray):
            ndarr = (img * 255 + 0.5).clip(0, 255).astype('uint8')
        elif isinstance(img, jt.Var):
            ndarr = (img * 255 + 0.5).clamp(0, 255).uint8().numpy()
        im = Image.fromarray(ndarr)
        im.save(path)

    def render_img(self, dataset_mode="train", img_id=None):
        W, H = self.image_resolutions
        H = int(H)
        W = int(W)
        if img_id is None:
            img_id = np.random.randint(0, self.dataset[dataset_mode].n_images, [1])[0]
            img_ids = jt.zeros([H * W], 'int32') + img_id
        else:
            img_ids = jt.zeros([H * W], 'int32') + img_id
        rays_o_total, rays_d_total, rays_pix_total = self.dataset[dataset_mode].generate_rays_total_test(
            img_ids, W, H)
        rays_pix_total = rays_pix_total.unsqueeze(-1)
        pixel = 0
        imgs = np.empty([H * W + self.n_rays_per_batch, 3])
        for pixel in range(0, W * H, self.n_rays_per_batch):
            end = pixel + self.n_rays_per_batch
            rays_o = rays_o_total[pixel:end]
            rays_d = rays_d_total[pixel:end]
            if end > H * W:
                rays_o = jt.concat(
                    [rays_o, jt.ones([end - H * W] + rays_o.shape[1:], rays_o.dtype)], dim=0)
                rays_d = jt.concat(
                    [rays_d, jt.ones([end - H * W] + rays_d.shape[1:], rays_d.dtype)], dim=0)

            pos, dir = self.sampler.sample(img_ids, rays_o, rays_d)
            network_outputs = self.model(pos, dir)
            rgb = self.sampler.rays2rgb(network_outputs, inference=True)
            imgs[pixel:end] = rgb.numpy()
        imgs = imgs[:H * W].reshape(H, W, 3)
        imgs_tar = jt.array(self.dataset[dataset_mode].image_data[img_id]).reshape(H, W, 4)
        imgs_tar = imgs_tar[..., :3] * imgs_tar[..., 3:] + jt.array(self.background_color) * (1 - imgs_tar[..., 3:])
        imgs_tar = imgs_tar.detach().numpy()
        jt.gc()
        return imgs, imgs_tar




exp_name={'1':'_Car','2':'_Coffee','3':'_Easyship','4':'_Scar','5':'_Scarf'}

for i in range(0,5,1):
  i = str(i+1)
  expnumber = exp_name[i]
  init_cfg("./projects/ngp/configs/ngp_comp" + expnumber + ".py")
  test = Runner()
  test.test(True)

for i in range(0,10):

 number = str(i)
 filePath = "logs/Easyship/test/"+"Easyship_r_"+number+".png"
 img = cv2.imread(filePath, flags=1)
 dst = cv2.fastNlMeansDenoisingColored(img, None, 60, 60, 7, 21)
 saveFile = "result/" + "Easyship_r_" + number + ".png"
 cv2.imwrite(saveFile, dst)

 filePath = "logs/Car/test/" + "Car_r_" + number + ".png"
 img = cv2.imread(filePath, flags=1)
 dst = cv2.fastNlMeansDenoisingColored(img, None, 60, 60, 7, 21)
 saveFile = "result/" + "Car_r_" + number + ".png"
 cv2.imwrite(saveFile, dst)

 filePath = "logs/Coffee/test/" + "Coffee_r_" + number + ".png"
 img = cv2.imread(filePath, flags=1)
 dst = cv2.fastNlMeansDenoisingColored(img, None, 3.7, 3.7, 7, 21)
 saveFile = "result/" + "Coffee_r_" + number + ".png"
 cv2.imwrite(saveFile, dst)

 filePath = "logs/Scar/test/" + "Scar_r_" + number + ".png"
 img = cv2.imread(filePath, flags=1)
 dst = cv2.fastNlMeansDenoisingColored(img, None, 3.8, 3.8, 7, 21)
 saveFile = "result/" + "Scar_r_" + number + ".png"
 cv2.imwrite(saveFile, dst)

 filePath = "logs/Scarf/test/" + "Scarf_r_" + number + ".png"
 img = cv2.imread(filePath, flags=1)
 dst = cv2.fastNlMeansDenoisingColored(img, None, 0.17, 0.17, 7, 21)
 saveFile = "result/" + "Scarf_r_" + number + ".png"

 cv2.imwrite(saveFile, dst)

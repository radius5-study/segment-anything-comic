from pytorch_lightning import Callback
import pickle
from osTools import *
from datamodule import * 
import random

def get_object_by_rel_path (obj, rel_path) : 
    paths = rel_path.split('.')
    for p in paths : 
        obj = getattr(obj, p)
    return obj

class VisualizePoints(Callback):

    def on_train_batch_end(self, trainer, pl_module, outputs, batch, batch_idx):
        global_step = trainer.global_step
        if global_step % 100 == 0 :
            save_path_base = osp.join(pl_module.logger.log_dir, 'images', 'train')
            mkdir(save_path_base)
            save_to = osp.join(save_path_base, f'img_{global_step}.png')
            visualize_batch(pl_module.sam_model, batch, trainer.datamodule.train_data, outputs=outputs, save_to=save_to)

    def on_validation_batch_end(self, trainer, pl_module, outputs, batch, batch_idx):
        global_step = trainer.global_step
        plot_val = random.random() < 0.05
        if plot_val or batch_idx in [2 ** x for x in range(20)] : 
            save_path_base = osp.join(pl_module.logger.log_dir, 'images', 'val')
            mkdir(save_path_base)
            save_to = osp.join(save_path_base, f'img_{global_step}_{batch_idx}.png')
            visualize_batch(pl_module.sam_model, batch, trainer.datamodule.test_data, outputs=outputs, save_to=save_to)

class ParameterTracker (Callback) : 
    """ 
    Sees how parameters are changing by logging the norms

    Helpful for tracking bugs in gradient setting

    """ 

    def __init__ (self, rel_paths, frequency=100) : 
        super().__init__() 
        self.rel_paths = rel_paths
        self.frequency = frequency

    @torch.no_grad()
    def on_train_batch_end(self, trainer, pl_module, outputs, batch, batch_idx):
        global_step = trainer.global_step
        if global_step % self.frequency == 0 :
            model_parts = [get_object_by_rel_path(pl_module, _) for _ in self.rel_paths]
            param_norms = [sum(p.norm() for p in part.parameters()) for part in model_parts]
            for pnorm, part_name in zip(param_norms, self.rel_paths) :
                trainer.logger.experiment.add_scalar(f'{part_name}_norm', pnorm, global_step)

class SaveArgs (Callback) : 

    def on_fit_start(self, trainer, pl_module):
        save_path = osp.join(pl_module.logger.log_dir, 'args.pkl')
        with open(save_path, 'wb') as fp : 
            pickle.dump(vars(pl_module.args), fp)

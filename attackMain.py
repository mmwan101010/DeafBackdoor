
from torch.utils.data import DataLoader
from scipy.io.wavfile import write
import torch
import torchaudio
import os
import numpy as np
import pickle

from dataset.Dataset import Dataset

from defense.defense import parser_defense

from model.iv_plda import iv_plda
from model.xv_plda import xv_plda
from model.audionet_csine import audionet_csine

from model.defended_model import defended_model

from attack.FGSM import FGSM
from attack.PGD import PGD
from attack.CWinf import CWinf
from attack.CW2 import CW2
from attack.FAKEBOB import FAKEBOB
from attack.SirenAttack import SirenAttack
from attack.Kenan import Kenan


device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
bits = 16

def parse_args():
    import argparse

    parser = argparse.ArgumentParser()

    subparser = parser.add_subparsers(dest='system_type') # either iv (ivector-PLDA) or xv (xvector-PLDA)

    iv_parser = subparser.add_parser("iv_plda")
    iv_parser.add_argument('-gmm', default='pre-trained-models/iv_plda/final_ubm.txt')
    iv_parser.add_argument('-extractor', default='pre-trained-models/iv_plda/final_ie.txt')
    iv_parser.add_argument('-plda', default='pre-trained-models/iv_plda/plda.txt')
    iv_parser.add_argument('-mean', default='pre-trained-models/iv_plda/mean.vec')
    iv_parser.add_argument('-transform', default='pre-trained-models/iv_plda/transform.txt')
    iv_parser.add_argument('-model_file', default='model_file/iv_plda/speaker_model_iv_plda')
    iv_parser.add_argument('-gmm_frame_bs', type=int, default=200)
    
    xv_parser = subparser.add_parser("xv_plda")
    xv_parser.add_argument('-extractor', default='pre-trained-models/xv_plda/xvecTDNN_origin.ckpt')
    xv_parser.add_argument('-plda', default='pre-trained-models/xv_plda/plda.txt')
    xv_parser.add_argument('-mean', default='pre-trained-models/xv_plda/mean.vec')
    xv_parser.add_argument('-transform', default='pre-trained-models/xv_plda/transform.txt')
    xv_parser.add_argument('-model_file', default='model_file/xv_plda/speaker_model_xv_plda')
    
    audionet_c_parser = subparser.add_parser("audionet_csine")
    audionet_c_parser.add_argument('-extractor', 
                default='pre-trained-models/audionet/cnn-natural-model-noise-0-002-50-epoch.pt.tmp8540_ckpt')
    audionet_c_parser.add_argument('-label_encoder', default='./label-encoder-audionet-Spk251_test.txt')

    # true threshold and threshold estimation
    parser.add_argument('-threshold', type=float, default=None) # for SV/OSI task; real threshold of the model
    parser.add_argument('-threshold_estimated', type=float, default=None) # for SV/OSI task; estimated threshold by FAKEBOB
    parser.add_argument('-thresh_est_wav_path', type=str, nargs='+', default=None) # the audio path used to estimate the threshold, should from imposter (initially rejected)
    parser.add_argument('-thresh_est_step', type=float, default=0.1) # the smaller, the accurate, but the slower
    
    #### add a defense layer in the model
    #### Note that for white-box attack, the defense method needs to be differentiable
    parser.add_argument('-defense', nargs='+', default=None)
    parser.add_argument('-defense_param', nargs='+', default=None)
    parser.add_argument('-defense_flag', nargs='+', default=None, type=int)
    parser.add_argument('-defense_order', default='sequential', choices=['sequential', 'average'])

    parser.add_argument('-root', type=str, required=True)
    parser.add_argument('-name', type=str, required=True)
    parser.add_argument('-des', type=str, default=None) # path to store adver audios
    parser.add_argument('-task', type=str, default='CSI', choices=['CSI', 'SV', 'OSI']) # the attack use this to set the loss function
    parser.add_argument('-wav_length', type=int, default=None)

    ## common attack parameters
    parser.add_argument('-targeted', action='store_true', default=False)
    parser.add_argument('-target_label_file', default=None) # the path of the file containing the target label; generated by set_target_label.py
    parser.add_argument('-batch_size', type=int, default=1)
    parser.add_argument('-EOT_size', type=int, default=1)
    parser.add_argument('-EOT_batch_size', type=int, default=1)
    parser.add_argument('-start', type=int, default=0)
    parser.add_argument('-end', type=int, default=-1)

    for system_type_parser in [iv_parser, xv_parser, audionet_c_parser]:
        
        subparser = system_type_parser.add_subparsers(dest='attacker')

        fgsm_parser = subparser.add_parser("FGSM")
        fgsm_parser.add_argument("-epsilon", type=float, default=0.002)
        fgsm_parser.add_argument('-loss', type=str, choices=['Entropy', 'Margin'], default='Entropy')

        pgd_parser = subparser.add_parser("PGD")
        pgd_parser.add_argument('-step_size', type=float, default=0.0004)
        pgd_parser.add_argument('-epsilon', type=float, default=0.002)
        pgd_parser.add_argument('-max_iter', type=int, default=10) # PGD-10 default
        pgd_parser.add_argument('-num_random_init', type=int, default=0)
        pgd_parser.add_argument('-loss', type=str, choices=['Entropy', 'Margin'], default='Entropy')

        cwinf_parser = subparser.add_parser("CWinf")
        cwinf_parser.add_argument('-step_size', type=float, default=0.001)
        cwinf_parser.add_argument('-epsilon', type=float, default=0.002)
        cwinf_parser.add_argument('-max_iter', type=int, default=10) # PGD-10 default
        cwinf_parser.add_argument('-num_random_init', type=int, default=0)

        cw2_parser = subparser.add_parser("CW2")
        cw2_parser.add_argument('-initial_const', type=float, default=1e-3)
        cw2_parser.add_argument('-binary_search_steps', type=int, default=9)
        cw2_parser.add_argument('-max_iter', type=int, default=10000)
        cw2_parser.add_argument('-stop_early', action='store_false', default=True)
        cw2_parser.add_argument('-stop_early_iter', type=int, default=1000)
        cw2_parser.add_argument('-lr', type=float, default=1e-2)
        cw2_parser.add_argument('-confidence', type=float, default=0.)
        # cw2_parser.add_argument('-dist_loss', default='L2', choices=['L2', 'PMSQE', 'PESQ'])

        fakebob_parser = subparser.add_parser("FAKEBOB")
        fakebob_parser.add_argument('-confidence', type=float, default=0.)
        fakebob_parser.add_argument("--epsilon", "-epsilon", default=0.002, type=float)
        fakebob_parser.add_argument("--max_iter", "-max_iter", default=1000, type=int)
        fakebob_parser.add_argument("--max_lr", "-max_lr", default=0.001, type=float)
        fakebob_parser.add_argument("--min_lr", "-min_lr", default=1e-6, type=float)
        fakebob_parser.add_argument("--samples_per_draw", "-samples", default=50, type=int)
        fakebob_parser.add_argument("--samples_batch", "-samples_batch", default=50, type=int)
        fakebob_parser.add_argument("--sigma", "-sigma", default=0.001, type=float)
        fakebob_parser.add_argument("--momentum", "-momentum", default=0.9, type=float)
        fakebob_parser.add_argument("--plateau_length", "-plateau_length", default=5, type=int)
        fakebob_parser.add_argument("--plateau_drop", "-plateau_drop", default=2.0, type=float)
        fakebob_parser.add_argument("--stop_early", "-stop_early", action='store_false', default=True)
        fakebob_parser.add_argument("--stop_early_iter", "-stop_early_iter", type=int, default=100)

        siren_parser = subparser.add_parser("SirenAttack")
        siren_parser.add_argument('-confidence', type=float, default=0.)
        siren_parser.add_argument("-epsilon", default=0.002, type=float)
        siren_parser.add_argument("-max_epoch", default=30, type=int)
        siren_parser.add_argument("-max_iter", default=300, type=int)
        siren_parser.add_argument("-c1", type=float, default=1.4961)
        siren_parser.add_argument("-c2", type=float, default=1.4961)
        siren_parser.add_argument("-n_particles", default=50, type=int)
        siren_parser.add_argument("-w_init", type=float, default=0.9)
        siren_parser.add_argument("-w_end", type=float, default=0.1)

        kenan_parser = subparser.add_parser("kenan")
        kenan_parser.add_argument("-atk_name", default='fft', type=str, choices=['fft', 'ssa'])
        kenan_parser.add_argument("-raster_width", default=100, type=int)
        kenan_parser.add_argument("-max_iter", default=15, type=int)
        kenan_parser.add_argument("-early_stop", type=int, default=0)

    args = parser.parse_args()
    return args

def save_audio(advers, names, root, fs=16000):
    for adver, name in zip(advers[:, 0, :], names):
        if 0.9 * adver.max() <= 1 and 0.9 * adver.min() >= -1:
            adver = adver * (2 ** (bits-1))
        if type(adver) == torch.Tensor:
            adver = adver.detach().cpu().numpy()
        adver = adver.astype(np.int16)
        spk_id = name.split("-")[0]
        spk_dir = os.path.join(root, spk_id)
        if not os.path.exists(spk_dir):
            os.makedirs(spk_dir)
        adver_path = os.path.join(spk_dir, name + ".wav")
        write(adver_path, fs, adver)
        

def main(args):

    # set up model
    if args.system_type == 'iv_plda':
        base_model = iv_plda(args.gmm, args.extractor, args.plda, args.mean, args.transform, device=device, 
                             model_file=args.model_file, threshold=args.threshold, gmm_frame_bs=args.gmm_frame_bs)
    elif args.system_type == 'xv_plda':
        base_model = xv_plda(args.extractor, args.plda, args.mean, args.transform, device=device, model_file=args.model_file, threshold=args.threshold)
    elif args.system_type == 'audionet_csine':
        base_model = audionet_csine(args.extractor, label_encoder=args.label_encoder, device=device)
    else:
        raise NotImplementedError('Unsupported System Type')
    
    defense, defense_name = parser_defense(args.defense, args.defense_param, args.defense_flag, args.defense_order)
    model = defended_model(base_model=base_model, defense=defense, order=args.defense_order)
    spk_ids = base_model.spk_ids
    
    wav_length = None if args.batch_size == 1 else args.wav_length
    # dataset = getattr(sys.modules[__name__], args.name)(spk_ids, root, return_file_name=True, wav_length=wav_length)
    # # set normalize to True since adv voices generated at [-1, 1] float domain
    dataset = Dataset(spk_ids, args.root, args.name, normalize=True, return_file_name=True, wav_length=wav_length)
    loader = DataLoader(dataset, batch_size=args.batch_size, num_workers=0)

    # deal with black-box threshold (in black-box attack, the attacker does not know the true threshold)
    BLACK_BOX_ATTACKS = ['FAKEBOB', 'SirenAttack']
    if args.task == 'SV' or args.task == 'OSI':
        if args.attacker in BLACK_BOX_ATTACKS:
            if args.attacker == 'SirenAttack':
                raise NotImplementedError('SirenAttack does not incorporate threshold estimation algorithm. \
                    Consider calling FAKEBOB to get the estimated threshold.')
            if args.threshold_estimated is None:
                fakebob = FAKEBOB(model, threshold=args.threshold_estimated, task=args.task, targeted=args.targeted, confidence=args.confidence,
                            epsilon=args.epsilon, max_iter=args.max_iter, max_lr=args.max_lr,
                            min_lr=args.min_lr, samples_per_draw=args.samples_per_draw, 
                            samples_per_draw_batch_size=args.samples_batch, sigma=args.sigma,
                            momentum=args.momentum, plateau_length=args.plateau_length,
                            plateau_drop=args.plateau_drop,
                            stop_early=args.stop_early, stop_early_iter=args.stop_early_iter,
                            batch_size=args.batch_size,
                            EOT_size=args.EOT_size, EOT_batch_size=args.EOT_batch_size,
                            verbose=1)
                assert args.thresh_est_wav_path is not None
                args.threshold_estimated = 0
                cnt = 0
                print('===== Estimating threshold using FAKEBOB =====')
                for path in args.thresh_est_wav_path:
                    wav, _ = torchaudio.load(path)
                    ll = fakebob.estimate_threshold(wav.unsqueeze(0).to(device), args.thresh_est_step)
                    if ll is not None:
                        args.threshold_estimated += ll
                        cnt += 1
                assert cnt > 0 # when cnt = 0, all the audios are not from imposter, cannot used to estimate the threshold
                args.threshold_estimated /= cnt
                print('===== Estimated threshold: {}, differ with true threshold: {} ====='.format(args.threshold_estimated,
                        abs(model.threshold - args.threshold_estimated)))

    attacker = None
    if args.attacker == 'FGSM':
        attacker = FGSM(model, task=args.task, epsilon=args.epsilon, loss=args.loss, targeted=args.targeted, 
                        batch_size=args.batch_size, EOT_size=args.EOT_size, EOT_batch_size=args.EOT_batch_size, verbose=1)
    elif args.attacker == 'PGD':
        attacker = PGD(model, task=args.task, targeted=args.targeted, step_size=args.step_size,
                       epsilon=args.epsilon, max_iter=args.max_iter,
                       batch_size=args.batch_size, num_random_init=args.num_random_init,
                       loss=args.loss, EOT_size=args.EOT_size, EOT_batch_size=args.EOT_batch_size, verbose=1)
    elif args.attacker == 'CWinf':
        attacker = CWinf(model, task=args.task, targeted=args.targeted, step_size=args.step_size,
                       epsilon=args.epsilon, max_iter=args.max_iter,
                       batch_size=args.batch_size, num_random_init=args.num_random_init,
                       loss=args.loss, EOT_size=args.EOT_size, EOT_batch_size=args.EOT_batch_size, verbose=1)
    elif args.attacker == 'CW2':
        attacker = CW2(model, task=args.task, initial_const=args.initial_const, binary_search_steps=args.binary_search_steps,
                            max_iter=args.max_iter, stop_early=args.stop_early, stop_early_iter=args.stop_early_iter, lr=args.lr,
                            targeted=args.targeted, confidence=args.confidence, verbose=1, batch_size=args.batch_size
                            )
    elif args.attacker == 'FAKEBOB':
        attacker = FAKEBOB(model, threshold=args.threshold_estimated, task=args.task, targeted=args.targeted, confidence=args.confidence,
                        epsilon=args.epsilon, max_iter=args.max_iter, max_lr=args.max_lr,
                        min_lr=args.min_lr, samples_per_draw=args.samples_per_draw, 
                        samples_per_draw_batch_size=args.samples_batch, sigma=args.sigma,
                        momentum=args.momentum, plateau_length=args.plateau_length,
                        plateau_drop=args.plateau_drop,
                        stop_early=args.stop_early, stop_early_iter=args.stop_early_iter,
                        batch_size=args.batch_size,
                        EOT_size=args.EOT_size, EOT_batch_size=args.EOT_batch_size,
                        verbose=1)
    elif args.attacker == 'SirenAttack':
        attacker = SirenAttack(model, threshold=args.threshold_estimated, 
                               task=args.task, targeted=args.targeted, confidence=args.confidence,
                               epsilon=args.epsilon, max_epoch=args.max_epoch, max_iter=args.max_iter,
                               c1=args.c1, c2=args.c2, n_particles=args.n_particles, w_init=args.w_init, w_end=args.w_end,
                               batch_size=args.batch_size, EOT_size=args.EOT_size, EOT_batch_size=args.EOT_batch_size,)
    elif args.attacker == 'kenan':
        attacker = Kenan(model, atk_name=args.atk_name, max_iter=args.max_iter, 
                        raster_width=args.raster_width, targeted=args.targeted, verbose=1, BITS=bits, 
                        early_stop=bool(args.early_stop), batch_size=args.batch_size)
    else:
        raise NotImplementedError('Not Supported Attack Algorithm')

    attacker_param = None
    if args.attacker == 'FGSM':
        attacker_param = [args.epsilon, args.EOT_size]
    elif args.attacker == 'PGD':
        attacker_param = [args.max_iter, args.epsilon, args.step_size, args.num_random_init, args.EOT_size]
    elif args.attacker == 'CWinf':
        attacker_param = [args.max_iter, args.epsilon, args.num_random_init, args.EOT_size]
    elif args.attacker == 'CW2':
        attacker_param = [args.initial_const, args.confidence, args.max_iter, args.stop_early_iter] 
    elif args.attacker == 'FAKEBOB':
        attacker_param = [args.epsilon, args.confidence, args.samples_per_draw, args.max_iter, args.stop_early_iter]
    elif args.attacker == 'SirenAttack':
        attacker_param = [args.epsilon, args.confidence, args.max_epoch, args.max_iter]
    elif args.attacker == 'kenan':
        attacker_param = "{}-{}".format(args.atk_name, args.max_iter)
    else:
        raise NotImplementedError('Not Supported Attack Algorithm')


    adver_dir = "./adver-audio/{}-{}-{}/{}/{}/{}-{}".format(args.system_type, args.task, args.name,
                defense_name, args.attacker, 
                args.attacker, attacker_param)
    if args.des is not None:
        adver_dir = args.des
    print(adver_dir),

    # load target label
    name2target = {}
    if args.target_label_file is not None:
        with open(args.target_label_file, 'rb') as reader:
            name2target = pickle.load(reader)
    
    start = min(max(args.start, 0), len(loader))
    end =  len(loader) if args.end == -1 else args.end
    end = min(max(end, 0), len(loader))
    print(start, end)

    success_cnt = 0
    for index, (origin, true, file_name) in enumerate(loader):
        if index not in range(start, end):
            continue

        des_path = os.path.join(adver_dir, file_name[0].split('-')[0], file_name[0] + '.wav')
        if os.path.exists(des_path):
            print('*' * 40, index, file_name[0], 'Exists, SKip', '*' * 40)
            continue

        origin = origin.to(device)
        true = true.to(device)
        if args.targeted:
            target = true.clone()
            for ii, y in enumerate(true):
                if file_name[ii] in name2target.keys():
                    target[ii] = name2target[file_name[ii]]
                else:
                    candidate_target_labels = list(range(len(spk_ids)))
                    if args.task == 'SV' or args.task == 'OSI':
                        candidate_target_labels.append(-1) # -1: reject
                    if y in candidate_target_labels:
                        candidate_target_labels.remove(y)
                    target[ii] = np.random.choice(candidate_target_labels)
            true = target
        print('*' * 10, index, '*' * 10)
        adver, success = attacker.attack(origin, true)
        save_audio(adver, file_name, adver_dir)
        success_cnt += sum(success)
    
    total_cnt = len(dataset)
    print(args.defense, args.defense_param, args.attacker, attacker_param, 'success rate: %f' % (success_cnt * 100 / total_cnt)) 

if __name__ == "__main__":

    main(parse_args())

import sys
import os
import logging
import traceback
import json
import subprocess
import time
import psutil
import shutil
import pynvml
import platform
import copy
import re
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QPushButton, QLabel, QComboBox, QCheckBox,
                             QFileDialog, QTextEdit, QMessageBox, QInputDialog,
                             QHBoxLayout, QGroupBox, QFormLayout, QLineEdit,
                             QDialog, QTableWidget, QTableWidgetItem, QHeaderView,
                             QTabWidget, QScrollArea, QToolButton, QSizePolicy, QDialogButtonBox)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QSize, QTimer, QMutex, QWaitCondition, pyqtSlot, QMetaObject, Q_ARG, QUrl
from PyQt5.QtGui import QFont, QIcon, QColor, QTextCharFormat, QTextCursor, QPainter, QPixmap, QDesktopServices, QFontInfo
import resources_rc
from archive import archive_folders
import tempfile

os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = "hide"
env = os.environ.copy()
logging.basicConfig(filename='msst_gui.log', level=logging.DEBUG,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

CONFIG_FILE = 'model_config_en.json'


def remove_screen_splash():
    # Use this code to signal the splash screen removal.
    logging.debug("Starting splash screen removal...")
    if "NUITKA_ONEFILE_PARENT" in os.environ:
        logging.debug(f"NUITKA_ONEFILE_PARENT: {os.environ['NUITKA_ONEFILE_PARENT']}")
        splash_filename = os.path.join(
            tempfile.gettempdir(),
            f"onefile_{int(os.environ['NUITKA_ONEFILE_PARENT'])}_splash_feedback.tmp"
        )
        logging.debug(f"Splash filename: {splash_filename}")
        if os.path.exists(splash_filename):
            try:
                os.unlink(splash_filename)
                logging.debug("Splash file removed successfully")
            except Exception as e:
                logging.error(f"Error removing splash file: {e}")
        else:
            logging.debug("Splash file does not exist")
    else:
        logging.debug("NUITKA_ONEFILE_PARENT not in environment variables")

    logging.debug("Splash screen removal complete")


def load_or_create_config():
    if not os.path.exists(CONFIG_FILE):
        initial_config = {
            "vocal_models": {
                "None": "Disable vocal separation",
                "MelBandRoformer_kim.ckpt": "[Rec] SDR≈1297&1296 but 2x faster, good for vocals & instrumental",
                "BS-Roformer-Resurrection.ckpt": "[Rec] Vocal specialist, preserves details/harmonies, high SDR(11.34)",
                "logic_roformer.pt": "[Rec] Multi-track separation (bass, drums, piano, guitar, vocals, others), best SDR for instruments",
                "mel_band_roformer_vocals_becruily.ckpt": "Vocal specialist using fullness metric (more detail but more noise)",
                "BS_ResurrectioN.ckpt": "Instrumental specialist, higher fullness but may leak pads to vocals",
                "inst_v1e.ckpt": "Instrumental specialist using fullness (closer to original sound)",
                "model_bs_roformer_ep_317_sdr_12.9755.ckpt": "Note: 1297 has slightly higher SDR but may add ultra-high freq noise",
                "model_bs_roformer_ep_368_sdr_12.9628.ckpt": "1296 has slightly lower SDR but no ultra-high freq noise",
                "big_beta5e.ckpt": "Very large model, highest fullness score for vocals (more noise)",
                "kimmel_unwa_ft2_bleedless.ckpt": "Kim model fine-tune, highest vocal bleedless score(39.30), slightly lower SDR/fullness"
            },
            "kara_models": {
                "None": "Disable harmony separation",
                "bs_roformer_karaoke_frazer_becruily.ckpt": "[Rec] Handles close harmonies well, conservative but good lead vocal detection",
                "mel_band_roformer_karaoke_becruily.ckpt": "Aggressive, better at separating harmonies, fuller sound",
                "mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt": "Aggressive, best performance, use reverb module if vocals damaged",
                "kar_gabox.ckpt": "Slightly conservative, similar performance, better with high pitches"
            },
            "reverb_models": {
                "None": "Disable reverb/harmony separation",
                "dereverb_mel_band_roformer_mono_anvuew_sdr_20.4029.ckpt": "[Rec] Highest SDR mono reverb removal, poor for harmonies",
                "dereverb_room_anvuew_sdr_13.7432.ckpt": "[Rec] Mono room reverb specialist, drier sound (mono input only)",
                "dereverb_echo_mbr_fused_0.5_v2_0.25_big_0.25_super.ckpt": "[Rec] Best for delay/echo removal, better with heavy reverb",
                "dereverb_mel_band_roformer_anvuew_sdr_19.1729.ckpt": "Previous best SDR, good harmony removal",
                "dereverb_mel_band_roformer_less_aggressive_anvuew_sdr_18.8050.ckpt": "Less aggressive than 1917, use if vocals damaged",
                "deverb_bs_roformer_8_384dim_10depth.ckpt": "New bs model, higher SDR, conservative on harmonies",
                "deverb_bs_roformer_8_256dim_8depth.ckpt": "Old bs model",
                "deverb_mel_band_roformer_8_256dim_6depth.ckpt": "Very aggressive, may damage vocals",
                "deverb_mel_band_roformer_8_512dim_12depth.ckpt": "Larger network, slightly higher SDR, 3x slower",
                "deverb_mel_band_roformer_ep_27_sdr_10.4567.ckpt": "Original model, balanced reverb/harmony removal"
            },
            "other_models": {
                "None": "Disable other modules",
                "denoise_mel_band_roformer_aufr33_sdr_27.9959.ckpt": "[Denoise] Standard version SDR 27.9959",
                "denoise_mel_band_roformer_aufr33_aggr_sdr_27.9768.ckpt": "[Denoise] Aggressive version SDR 27.9768",
                "model_bandit_plus_dnr_sdr_11.47.chpt": "[Denoise] Removes mouse/keyboard/effects, may remove lead vocals",
                "bleed_suppressor_v1.ckpt": "[Denoise] Suppresses leakage for fullness models",
                "Apollo_LQ_MP3_restoration.ckpt": "[Restore] MP3 quality restoration to 44.1 kHz",
                "aspiration_mel_band_roformer_sdr_18.9845.ckpt": "[Breath] Separates breath sounds",
                "aspiration_mel_band_roformer_less_aggr_sdr_18.1201.ckpt": "[Breath] Less aggressive breath separation",
                "mel_band_roformer_crowd_aufr33_viperx_sdr_8.7144.ckpt": "[Denoise] Background speech separation, affects quality",
                "bs_roformer_male_female_by_aufr33_sdr_7.2889.ckpt": "[Separation] Separates simultaneous male/female speech"
            },
            "config_paths": {
                "MelBandRoformer_kim.ckpt": [
                    "configs/config_vocals_mel_band_roformer_kim.yaml",
                    "configs/config_vocals_mel_band_roformer_kim-fast.yaml"
                ],
                "model_bs_roformer_ep_317_sdr_12.9755.ckpt": [
                    "configs/model_bs_roformer_ep_317_sdr_12.9755.yaml",
                    "configs/model_bs_roformer_ep_317_sdr_12.9755-fast.yaml"
                ],
                "model_bs_roformer_ep_368_sdr_12.9628.ckpt": [
                    "configs/model_bs_roformer_ep_368_sdr_12.9628.yaml",
                    "configs/model_bs_roformer_ep_368_sdr_12.9628-fast.yaml"
                ],
                "mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt": [
                    "configs/config_mel_band_roformer_karaoke.yaml",
                    "configs/config_mel_band_roformer_karaoke-fast.yaml"
                ],
                "dereverb_mel_band_roformer_anvuew_sdr_19.1729.ckpt": [
                    "configs/dereverb_mel_band_roformer_anvuew.yaml",
                    "configs/dereverb_mel_band_roformer_anvuew-fast.yaml"
                ],
                "dereverb_mel_band_roformer_less_aggressive_anvuew_sdr_18.8050.ckpt": [
                    "configs/dereverb_mel_band_roformer_anvuew.yaml",
                    "configs/dereverb_mel_band_roformer_anvuew-fast.yaml"
                ],
                "deverb_bs_roformer_8_384dim_10depth.ckpt": [
                    "configs/deverb_bs_roformer_8_384dim_10depth.yaml",
                    "configs/deverb_bs_roformer_8_384dim_10depth-fast.yaml"
                ],
                "deverb_bs_roformer_8_256dim_8depth.ckpt": [
                    "configs/deverb_bs_roformer_8_256dim_8depth.yaml",
                    "configs/deverb_bs_roformer_8_256dim_8depth-fast.yaml"
                ],
                "deverb_mel_band_roformer_8_256dim_6depth.ckpt": [
                    "configs/8_256_6_deverb_mel_band_roformer_8_256dim_6depth.yaml",
                    "configs/8_256_6_deverb_mel_band_roformer_8_256dim_6depth-fast.yaml"
                ],
                "deverb_mel_band_roformer_8_512dim_12depth.ckpt": [
                    "configs/8_512_12_deverb_mel_band_roformer_8_512dim_12depth.yaml",
                    "configs/8_512_12_deverb_mel_band_roformer_8_512dim_12depth-fast.yaml"
                ],
                "deverb_mel_band_roformer_ep_27_sdr_10.4567.ckpt": [
                    "configs/deverb_mel_band_roformer.yaml",
                    "configs/deverb_mel_band_roformer-fast.yaml"
                ],
                "denoise_mel_band_roformer_aufr33_sdr_27.9959.ckpt": [
                    "configs/model_mel_band_roformer_denoise.yaml",
                    "configs/model_mel_band_roformer_denoise-fast.yaml"
                ],
                "denoise_mel_band_roformer_aufr33_aggr_sdr_27.9768.ckpt": [
                    "configs/model_mel_band_roformer_denoise.yaml",
                    "configs/model_mel_band_roformer_denoise-fast.yaml"
                ],
                "Apollo_LQ_MP3_restoration.ckpt": [
                    "configs/config_apollo_LQ_MP3_restoration.yaml",
                    "configs/config_apollo_LQ_MP3_restoration-fast.yaml"
                ],
                "aspiration_mel_band_roformer_sdr_18.9845.ckpt": [
                    "configs/config_aspiration_mel_band_roformer.yaml",
                    "configs/config_aspiration_mel_band_roformer-fast.yaml"
                ],
                "aspiration_mel_band_roformer_less_aggr_sdr_18.1201.ckpt": [
                    "configs/config_aspiration_mel_band_roformer.yaml",
                    "configs/config_aspiration_mel_band_roformer-fast.yaml"
                ],
                "mel_band_roformer_crowd_aufr33_viperx_sdr_8.7144.ckpt": [
                    "configs/model_mel_band_roformer_crowd_aufr33_viperx.yaml",
                    "configs/model_mel_band_roformer_crowd_aufr33_viperx-fast.yaml"
                ],
                "mel_band_roformer_vocals_becruily.ckpt": [
                    "configs/config_vocals_becruily.yaml",
                    "configs/config_vocals_becruily-fast.yaml"
                ],
                "inst_v1e.ckpt": [
                    "configs/config_melbandroformer_inst.yaml",
                    "configs/config_melbandroformer_inst-fast.yaml"
                ],
                "big_beta5e.ckpt": [
                    "configs/big_beta5e.yaml",
                    "configs/big_beta5e-fast.yaml"
                ],
                "bleed_suppressor_v1.ckpt": [
                    "configs/config_bleed_suppressor_v1.yaml",
                    "configs/config_bleed_suppressor_v1-fast.yaml"
                ],
                "dereverb_echo_mbr_fused_0.5_v2_0.25_big_0.25_super.ckpt": [
                    "configs/config_dereverb_echo_mbr_v2.yaml",
                    "configs/config_dereverb_echo_mbr_v2-fast.yaml"
                ],
                "dereverb_mel_band_roformer_mono_anvuew_sdr_20.4029.ckpt": [
                    "configs/dereverb_mel_band_roformer_anvuew.yaml",
                    "configs/dereverb_mel_band_roformer_anvuew-fast.yaml"
                ],
                "bs_roformer_male_female_by_aufr33_sdr_7.2889.ckpt": [
                    "configs/config_chorus_male_female_bs_roformer.yaml",
                    "configs/config_chorus_male_female_bs_roformer-fast.yaml"
                ],
                "kar_gabox.ckpt": [
                    "configs/config_mel_band_roformer_karaoke.yaml",
                    "configs/config_mel_band_roformer_karaoke-fast.yaml"
                ],
                "model_bandit_plus_dnr_sdr_11.47.chpt": [
                    "configs/config_dnr_bandit_bsrnn_multi_mus64.yaml",
                    "configs/config_dnr_bandit_bsrnn_multi_mus64-fast.yaml"
                ],
                "kimmel_unwa_ft2_bleedless.ckpt": [
                    "configs/config_kimmel_unwa_ft.yaml",
                    "configs/config_kimmel_unwa_ft-fast.yaml"
                ],
                "mel_band_roformer_karaoke_becruily.ckpt": [
                    "configs/config_karaoke_becruily.yaml",
                    "configs/config_karaoke_becruily-fast.yaml"
                ],
                "BS_ResurrectioN.ckpt": [
                    "configs/BS-Roformer-Resurrection-Inst-Config.yaml",
                    "configs/BS-Roformer-Resurrection-Inst-Config-fast.yaml"
                ],
                "logic_roformer.pt": [
                    "configs/logic_pro_config_v1.yaml",
                    "configs/logic_pro_config_v1-fast.yaml"
                ],
                "bs_roformer_karaoke_frazer_becruily.ckpt": [
                    "configs/config_karaoke_frazer_becruily.yaml",
                    "configs/config_karaoke_frazer_becruily-fast.yaml"
                ],
                "dereverb_room_anvuew_sdr_13.7432.ckpt": [
                    "configs/dereverb_room_anvuew.yaml",
                    "configs/dereverb_room_anvuew-fast.yaml"
                ],
                "BS-Roformer-Resurrection.ckpt": [
                    "configs/BS-Roformer-Resurrection-Config.yaml",
                    "configs/BS-Roformer-Resurrection-Config-fast.yaml"
                ]
            },
            "model_types": {
                "MelBandRoformer_kim.ckpt": "mel_band_roformer",
                "model_bs_roformer_ep_317_sdr_12.9755.ckpt": "bs_roformer",
                "model_bs_roformer_ep_368_sdr_12.9628.ckpt": "bs_roformer",
                "mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt": "mel_band_roformer",
                "dereverb_mel_band_roformer_anvuew_sdr_19.1729.ckpt": "mel_band_roformer",
                "dereverb_mel_band_roformer_less_aggressive_anvuew_sdr_18.8050.ckpt": "mel_band_roformer",
                "deverb_bs_roformer_8_384dim_10depth.ckpt": "bs_roformer",
                "deverb_bs_roformer_8_256dim_8depth.ckpt": "bs_roformer",
                "deverb_mel_band_roformer_8_256dim_6depth.ckpt": "mel_band_roformer",
                "deverb_mel_band_roformer_8_512dim_12depth.ckpt": "mel_band_roformer",
                "deverb_mel_band_roformer_ep_27_sdr_10.4567.ckpt": "mel_band_roformer",
                "denoise_mel_band_roformer_aufr33_sdr_27.9959.ckpt": "mel_band_roformer",
                "denoise_mel_band_roformer_aufr33_aggr_sdr_27.9768.ckpt": "mel_band_roformer",
                "Apollo_LQ_MP3_restoration.ckpt": "apollo",
                "aspiration_mel_band_roformer_sdr_18.9845.ckpt": "mel_band_roformer",
                "aspiration_mel_band_roformer_less_aggr_sdr_18.1201.ckpt": "mel_band_roformer",
                "mel_band_roformer_crowd_aufr33_viperx_sdr_8.7144.ckpt": "mel_band_roformer",
                "mel_band_roformer_vocals_becruily.ckpt": "mel_band_roformer",
                "inst_v1e.ckpt": "mel_band_roformer",
                "big_beta5e.ckpt": "mel_band_roformer",
                "bleed_suppressor_v1.ckpt": "mel_band_roformer",
                "dereverb_echo_mbr_fused_0.5_v2_0.25_big_0.25_super.ckpt": "mel_band_roformer",
                "dereverb_mel_band_roformer_mono_anvuew_sdr_20.4029.ckpt": "mel_band_roformer",
                "bs_roformer_male_female_by_aufr33_sdr_7.2889.ckpt": "bs_roformer",
                "kar_gabox.ckpt": "mel_band_roformer",
                "model_bandit_plus_dnr_sdr_11.47.chpt": "bandit",
                "kimmel_unwa_ft2_bleedless.ckpt": "mel_band_roformer",
                "mel_band_roformer_karaoke_becruily.ckpt": "mel_band_roformer",
                "BS_ResurrectioN.ckpt": "bs_roformer",
                "logic_roformer.pt": "bs_roformer",
                "bs_roformer_karaoke_frazer_becruily.ckpt": "bs_roformer",
                "dereverb_room_anvuew_sdr_13.7432.ckpt": "bs_roformer",
                "BS-Roformer-Resurrection.ckpt": "bs_roformer"
            },
            "main_tracks": {
                "BS_ResurrectioN.ckpt": "instrumental",
                "logic_roformer.pt": "vocals",
                "bs_roformer_karaoke_frazer_becruily.ckpt": "Vocals",
                "dereverb_room_anvuew_sdr_13.7432.ckpt": "noreverb",
                "MelBandRoformer_kim.ckpt": "vocals",
                "mel_band_roformer_vocals_becruily.ckpt": "vocals",
                "inst_v1e.ckpt": "instrumental",
                "model_bs_roformer_ep_317_sdr_12.9755.ckpt": "Vocals",
                "model_bs_roformer_ep_368_sdr_12.9628.ckpt": "Vocals",
                "big_beta5e.ckpt": "vocals",
                "kimmel_unwa_ft2_bleedless.ckpt": "vocals",
                "mel_band_roformer_karaoke_becruily.ckpt": "Vocals",
                "mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt": "karaoke",
                "kar_gabox.ckpt": "karaoke",
                "dereverb_mel_band_roformer_mono_anvuew_sdr_20.4029.ckpt": "noreverb",
                "dereverb_echo_mbr_fused_0.5_v2_0.25_big_0.25_super.ckpt": "dry",
                "dereverb_mel_band_roformer_anvuew_sdr_19.1729.ckpt": "noreverb",
                "dereverb_mel_band_roformer_less_aggressive_anvuew_sdr_18.8050.ckpt": "noreverb",
                "deverb_bs_roformer_8_384dim_10depth.ckpt": "noreverb",
                "deverb_bs_roformer_8_256dim_8depth.ckpt": "noreverb",
                "deverb_mel_band_roformer_8_256dim_6depth.ckpt": "noreverb",
                "deverb_mel_band_roformer_8_512dim_12depth.ckpt": "noreverb",
                "deverb_mel_band_roformer_ep_27_sdr_10.4567.ckpt": "noreverb",
                "denoise_mel_band_roformer_aufr33_sdr_27.9959.ckpt": "dry",
                "denoise_mel_band_roformer_aufr33_aggr_sdr_27.9768.ckpt": "dry",
                "model_bandit_plus_dnr_sdr_11.47.chpt": "speech",
                "bleed_suppressor_v1.ckpt": "instrumental",
                "Apollo_LQ_MP3_restoration.ckpt": "restored",
                "aspiration_mel_band_roformer_sdr_18.9845.ckpt": "other",
                "aspiration_mel_band_roformer_less_aggr_sdr_18.1201.ckpt": "other",
                "mel_band_roformer_crowd_aufr33_viperx_sdr_8.7144.ckpt": "instrumental",
                "bs_roformer_male_female_by_aufr33_sdr_7.2889.ckpt": "female",
                "BS-Roformer-Resurrection.ckpt": "vocals"
            },
            "inference_env": ".\\env\\python.exe"
        }
        with open(CONFIG_FILE, 'w') as f:
            json.dump(initial_config, f, ensure_ascii=False, indent=4)

    with open(CONFIG_FILE, 'r') as f:
        return json.load(f)


def organize_instrumental_files(store_dir, main_track):
    if not os.path.exists(store_dir):
        return 0, 0.0

    moved_files = 0
    start_time = time.time()

    instrumental_dir = os.path.join(store_dir, "instrumental")
    if not os.path.exists(instrumental_dir):
        os.makedirs(instrumental_dir)

    for item in os.listdir(store_dir):
        item_path = os.path.join(store_dir, item)

        if os.path.isdir(item_path) and item != "instrumental":
            audio_name = item

            for track_file in os.listdir(item_path):
                if track_file.endswith(('.wav', '.mp3', '.flac')):
                    track_name = os.path.splitext(track_file)[0]
                    src_path = os.path.join(item_path, track_file)
                    new_filename = f"{audio_name}_{track_file}"

                    if track_name == main_track:
                        dst_path = os.path.join(store_dir, new_filename)
                    else:
                        audio_instrumental_dir = os.path.join(instrumental_dir, audio_name)
                        if not os.path.exists(audio_instrumental_dir):
                            os.makedirs(audio_instrumental_dir)
                        dst_path = os.path.join(audio_instrumental_dir, new_filename)

                    try:
                        shutil.move(src_path, dst_path)
                        moved_files += 1
                    except (PermissionError, OSError) as e:
                        logger.warning(f"Unable to move the file {src_path}: {str(e)}")
            try:
                if not os.listdir(item_path):
                    os.rmdir(item_path)
            except OSError:
                pass  # The directory may not be empty or cannot be deleted, temporarily retained.

    end_time = time.time()
    logger.info(f"Sorting completed: organized {moved_files} instrumental files in {end_time - start_time:.2f} s")
    return moved_files, end_time - start_time


class ScalingUtils:
    @staticmethod
    def get_scaling_factor():
        try:
            screen = QApplication.primaryScreen()
            if screen:
                dpi = screen.logicalDotsPerInch()
                # Calculate the scaling_factor based on 96 DPI (100%)
                scaling_factor = dpi / 96.0
                return max(0.5, min(3.0, scaling_factor))
        except:
            pass
        return 1.0

    @staticmethod
    def scale_size(size, scaling_factor):
        return int(round(size * scaling_factor))

    @staticmethod
    def scale_font(font, scaling_factor):
        scaled_font = QFont(font)
        if scaling_factor != 1.0:
            new_size = max(8, int(round(font.pointSizeF() * scaling_factor)))
            scaled_font.setPointSize(new_size)
        return scaled_font

    @staticmethod
    def scale_stylesheet(stylesheet, scaling_factor=None):
        if scaling_factor is None:
            scaling_factor = ScalingUtils.get_scaling_factor()

        if scaling_factor == 1.0:
            return stylesheet

        pattern = r'(?<!padding)(?<!padding-top)(?<!padding-right)(?<!padding-bottom)(?<!padding-left)(?<!-)\s*:\s*(\d+)(px|pt|em|ex|%|in|cm|mm|pc)'

        def scale_match(match):
            full_match = match.group(0)
            original_size = int(match.group(1))
            unit = match.group(2)

            if unit == 'px':
                scaled_size = ScalingUtils.scale_size(original_size, scaling_factor)
                return re.sub(r'\d+' + unit, f"{scaled_size}{unit}", full_match)
            else:
                return full_match

        scaled_stylesheet = re.sub(pattern, scale_match, stylesheet)
        compound_pattern = r'(?<!padding)(?<!padding-top)(?<!padding-right)(?<!padding-bottom)(?<!padding-left)(?<!-)\s*:\s*(\d+)px\s+(\d+)px'

        def scale_compound_match(match):
            full_match = match.group(0)
            size1 = ScalingUtils.scale_size(int(match.group(1)), scaling_factor)
            size2 = ScalingUtils.scale_size(int(match.group(2)), scaling_factor)
            return re.sub(r'\d+px\s+\d+px', f"{size1}px {size2}px", full_match)

        scaled_stylesheet = re.sub(compound_pattern, scale_compound_match, scaled_stylesheet)
        return scaled_stylesheet

    @staticmethod
    def set_scaled_stylesheet(widget, stylesheet, scaling_factor=None):
        scaled_stylesheet = ScalingUtils.scale_stylesheet(stylesheet, scaling_factor)
        widget.setStyleSheet(scaled_stylesheet)


class SystemInfoThread(QThread):
    info_signal = pyqtSignal(str, str, bool, bool, bool)

    def __init__(self):
        super().__init__()
        self.mutex = QMutex()
        self.condition = QWaitCondition()
        self.text_queue = []
        self.is_running = True

    def run(self):
        self.get_system_info()
        while self.is_running:
            self.mutex.lock()
            if not self.text_queue:
                self.condition.wait(self.mutex)
            if self.text_queue:
                text, color, bold, italic, auto_newline, delay = self.text_queue.pop(0)
                self.mutex.unlock()
                for char in text:
                    if not self.is_running:
                        return
                    self.info_signal.emit(char, color, bold, italic, False)
                    self.msleep(delay)
                if auto_newline:
                    self.info_signal.emit('\n', color, False, False, False)
            else:
                self.mutex.unlock()

    @staticmethod
    def get_cpu_info():
        if platform.system() == "Windows":
            try:
                output = subprocess.check_output("wmic cpu get name", shell=True).decode('utf-8').strip()
                lines = output.split('\n')
                if len(lines) > 1:
                    return lines[1]  # The first line is the header, the second line is the CPU name
            except subprocess.CalledProcessError as e:
                logger.error(f"Error executing WMIC command: {str(e)}")
            except Exception as e:
                logger.error(f"Error getting CPU info: {str(e)}")
        return platform.processor()

    def get_system_info(self):
        self.print_with_delay("System Information:", color='gray', bold=True)
        self.print_with_delay("=" * 50, color='gray')

        cpu_info = self.get_cpu_info()
        cpu_info_str = f"CPU: {cpu_info}"
        self.print_with_delay(cpu_info_str, color='#ffc0cb')

        ram = psutil.virtual_memory()
        ram_total_gb = ram.total / (1024 ** 3)
        ram_info = f"RAM: {ram.total / (1024 ** 3):.2f} GB (Used: {ram.percent}%)"
        self.print_with_delay(ram_info, color='#ffc0cb')

        if ram_total_gb < 16:
            ram_warning = "Warning: Available RAM is less than 16GB. Consider enabling virtual memory for better performance."
        else:
            ram_warning = "Available RAM is sufficient for inference."

        gpu_info = "GPU: Not detected or unsupported"
        gpu_warning = ""

        try:
            pynvml.nvmlInit()
            deviceCount = pynvml.nvmlDeviceGetCount()
            if deviceCount > 0:
                handle = pynvml.nvmlDeviceGetHandleByIndex(0)

                gpu_name = pynvml.nvmlDeviceGetName(handle)
                if isinstance(gpu_name, bytes):
                    gpu_name = gpu_name.decode('utf-8')

                memory_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                total_memory = memory_info.total / (1024 * 1024)
                used_memory = memory_info.used / (1024 * 1024)
                free_memory = memory_info.free / (1024 * 1024)

                driver_version = pynvml.nvmlSystemGetDriverVersion()
                if isinstance(driver_version, bytes):
                    driver_version = driver_version.decode('utf-8')

                gpu_info = (
                    f"GPU: {gpu_name}\n"
                    f"VRAM: {total_memory:.0f} MB (Used: {used_memory:.0f} MB)\n"
                    f"Driver Version: {driver_version}"
                )

                if free_memory < 2000:
                    gpu_warning = "Warning：Available GPU memory is less than 2GB. Please use CPU for inference."
                elif free_memory < 4000:
                    gpu_warning = "Warning: Available GPU memory is less than 4GB. This may cause OOM."
                else:
                    gpu_warning = "GPU memory is sufficient for inference."
            else:
                gpu_warning = "No NVIDIA GPU detected. Please use CPU for inference."

            pynvml.nvmlShutdown()
        except Exception as e:
            gpu_warning = f"检测显卡时出错: {str(e)}. \n请开启CPU推理"

        self.print_with_delay(gpu_info, color='#ffc0cb')
        self.print_with_delay("=" * 50, color='gray')
        self.print_with_delay(gpu_warning, color='#ffa500')
        self.print_with_delay(ram_warning, color='#ffa500')
        self.print_with_delay("=" * 50, color='gray')
        self.print_with_delay("Github: https://github.com/AliceNavigator/Music-Source-Separation-Training-GUI", color='green', auto_newline=False)

    @pyqtSlot(str, str, bool, bool, bool, int)
    def print_with_delay(self, text, color='white', bold=False, italic=False, auto_newline=True, delay=10):
        self.mutex.lock()
        self.text_queue.append((text, color, bold, italic, auto_newline, delay))
        self.condition.wakeOne()
        self.mutex.unlock()

    def stop(self):
        self.is_running = False
        self.condition.wakeOne()


class CustomComboBox(QComboBox):
    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setPen(QColor("#4CAF50"))
        painter.drawText(self.rect().adjusted(0, 0, -5, 0), Qt.AlignRight | Qt.AlignVCenter, "▼")


class InferenceThread(QThread):
    update_signal = pyqtSignal(str, bool)  # bool use for tqdm
    finished_signal = pyqtSignal(dict)
    file_organization_signal = pyqtSignal(int, float)

    def __init__(self, commands, input_folder):
        super().__init__()
        self.commands = commands
        self.input_folder = input_folder
        self.is_running = True
        self.process = None

    @staticmethod
    def extract_env_path(command):
        python_exe_index = command.find("python.exe ")
        if python_exe_index != -1:
            env_path = command[:python_exe_index]
            if not env_path.endswith('\\') and not env_path.endswith('/'):
                env_path += '\\'
            return env_path
        else:
            return None

    def run(self):
        start_time = time.time()
        summary = {
            "total_files": 0,
            "modules": [],
            "errors": 0
        }

        module_names = {
            "separation_results": "Vocal Model",
            "karaoke_results": "Karaoke Model",
            "deverb_results": "Reverb Model",
            "other_results": "Other Model"
        }

        total_files = sum(len(files) for _, _, files in os.walk(self.input_folder))
        summary["total_files"] = total_files
        logger.info(f"Total files in input folder: {total_files}")

        for command, store_dir in self.commands:
            if not self.is_running:
                break
            logger.info(f"Starting inference with command: {command}")
            self.update_signal.emit(f"Module: {module_names[store_dir]}", False)
            self.update_signal.emit(f"Command: {command}", False)
            env_path = self.extract_env_path(command)
            new_paths = f'{env_path}Scripts;{env_path}bin;{env_path};'
            if new_paths not in env['PATH']:
                env['PATH'] = new_paths + env['PATH']
            self.process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            text=True, universal_newlines=True, env=env)
            for line in self.process.stdout:
                if not self.is_running:
                    break
                stripped_line = line.strip()
                if line.startswith('\r') or "it/s]" in line:  # if is tqdm
                    self.update_signal.emit(stripped_line, True)
                else:
                    self.update_signal.emit(stripped_line, False)
                logger.debug(stripped_line)
                if "error" in line.lower():
                    summary["errors"] += 1
            if self.is_running:
                self.process.wait()
                summary["modules"].append((module_names[store_dir], store_dir))
                logger.info(f"Module {module_names[store_dir]} completed. ")

                model_name = self.get_current_model_name(command)
                main_track = self.get_main_track_for_model(model_name)
                moved_files, time_taken = organize_instrumental_files(store_dir, main_track)
                self.file_organization_signal.emit(moved_files, time_taken)
            else:
                self.terminate_process()
            logger.info(f"Inference process completed or terminated for {module_names[store_dir]}")

        if self.is_running:
            summary["total_time"] = time.time() - start_time
            logger.info(
                f"Inference completed. Total files: {summary['total_files']}, Time: {summary['total_time']:.2f} seconds")
            self.finished_signal.emit(summary)
        else:
            self.update_signal.emit("Inference process was terminated.", False)

    @staticmethod
    def get_current_model_name(command):
        # Match paths with quotes (can handle spaces)
        quoted_match = re.search(r'--start_check_point\s+[\'"]([^\'"]+)[\'"]', command)
        if quoted_match:
            return os.path.basename(quoted_match.group(1))

        # Match paths without quotes (cannot handle spaces)
        unquoted_match = re.search(r'--start_check_point\s+([^\s\'"]+)', command)
        if unquoted_match:
            return os.path.basename(unquoted_match.group(1))
        return None

    @staticmethod
    def get_main_track_for_model(model_name):
        if not model_name or model_name == "None":
            return "vocals"  # Default

        config = load_or_create_config()
        main_tracks = config.get("main_tracks", {})
        if model_name in main_tracks:
            return main_tracks[model_name]

        return "vocals"

    def stop(self):
        self.is_running = False
        if self.process:
            self.terminate_process()

    def terminate_process(self):
        logger.info("Terminating inference process")
        if self.process:
            try:
                parent = psutil.Process(self.process.pid)
                children = parent.children(recursive=True)
                for child in children:
                    child.terminate()
                parent.terminate()
                gone, still_alive = psutil.wait_procs(children + [parent], timeout=5)
                for p in still_alive:
                    p.kill()
            except psutil.NoSuchProcess:
                pass
        self.process = None


class ModelEditDialog(QDialog):
    def __init__(self, model_name, model_info, config, parent=None):
        super().__init__(parent)
        self.main_track_edit = None
        self.cancel_button = None
        self.save_button = None
        self.model_type_edit = None
        self.fast_config_path_edit = None
        self.config_path_edit = None
        self.description_edit = None
        self.model_name = model_name
        self.model_info = model_info
        self.config = config
        self.setWindowTitle(f"Edit Model: {model_name}")
        self.scaling_factor = ScalingUtils.get_scaling_factor()
        base_width = 800
        base_height = 500
        scaled_width = ScalingUtils.scale_size(base_width, self.scaling_factor)
        scaled_height = ScalingUtils.scale_size(base_height, self.scaling_factor)
        self.setGeometry(100, 100, scaled_width, scaled_height)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(20)
        layout.setContentsMargins(30, 30, 30, 30)

        form_layout = QFormLayout()
        form_layout.setSpacing(15)

        self.description_edit = QTextEdit(self.model_info)
        self.description_edit.setFixedHeight(100)
        form_layout.addRow(QLabel("Description:"), self.description_edit)

        self.config_path_edit = QLineEdit(self.config.get("config_paths", {}).get(self.model_name, ["", ""])[0])
        form_layout.addRow(QLabel("Config Path:"), self.config_path_edit)

        self.fast_config_path_edit = QLineEdit(self.config.get("config_paths", {}).get(self.model_name, ["", ""])[1])
        form_layout.addRow(QLabel("Fast Config Path:"), self.fast_config_path_edit)

        self.model_type_edit = QLineEdit(self.config.get("model_types", {}).get(self.model_name, ""))
        form_layout.addRow(QLabel("Model Type:"), self.model_type_edit)

        self.main_track_edit = QLineEdit(self.config.get("main_tracks", {}).get(self.model_name, ""))
        form_layout.addRow(QLabel("Target track:"), self.main_track_edit)

        layout.addLayout(form_layout)

        explanation_text = """
        <b>Description:</b> A brief explanation of the model's purpose or characteristics.<br>
        <b>Config Path:</b> The path to the model's configuration file for normal inference.<br>
        <b>Fast Config Path:</b> The path to the model's configuration file for fast inference mode.<br>
        <b>Model Type:</b> The type of the model (e.g., mel_band_roformer, bs_roformer).<br>
        <b>Target track:</b> The name of the target track output by this model, used to determine the input track for cascaded inference. If left blank, "vocals" will be used.
        """
        explanation_label = QLabel(explanation_text)
        explanation_label.setWordWrap(True)
        explanation_label.setStyleSheet("background-color: #f0f4f8; padding: 15px; border-radius: 10px; color: #333;")
        layout.addWidget(explanation_label)

        # save cancel button
        button_widget = QWidget()
        button_layout = QHBoxLayout(button_widget)
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.setSpacing(10)

        self.save_button = QPushButton("Save")
        self.cancel_button = QPushButton("Cancel")
        self.save_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)

        button_layout.addWidget(self.save_button)
        button_layout.addWidget(self.cancel_button)

        layout.addWidget(button_widget, 0, Qt.AlignRight | Qt.AlignBottom)

        model_edit_dialog_stylesheet = """
            ModelEditDialog {
                background-color: #ffffff;
                border-radius: 10px;
            }
            ModelEditDialog QLabel {
                font-size: 14px;
                color: #333;
            }
            ModelEditDialog QLineEdit, ModelEditDialog QTextEdit {
                border: 1px solid #ddd;
                border-radius: 5px;
                padding: 4px;
                background-color: #f9f9f9;
                font-size: 14px;
                color: #333;
            }
            ModelEditDialog QPushButton {
                background-color: #4a90e2;
                color: white;
                border: none;
                padding: 8px 16px;
                text-align: center;
                text-decoration: none;
                font-size: 14px;
                border-radius: 4px;
                min-width: 80px;
            }
            ModelEditDialog QPushButton:hover {
            background-color: #357abd;
            }
            ModelEditDialog QPushButton:pressed {
            background-color: #2a5d8b;
            }
        """
        ScalingUtils.set_scaled_stylesheet(self, model_edit_dialog_stylesheet, self.scaling_factor)
        MainWindow.center_on_screen(self)

    def get_updated_info(self):
        return {
            "description": self.description_edit.toPlainText(),
            "config_path": self.config_path_edit.text(),
            "fast_config_path": self.fast_config_path_edit.text(),
            "model_type": self.model_type_edit.text(),
            "main_track": self.main_track_edit.text()
        }


class ConfigEditorDialog(QDialog):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.cancel_button = None
        self.save_button = None
        self.tabs = None
        self.background_label = None
        self.original_config = config
        self.working_config = copy.deepcopy(self.original_config)
        self.working_config = self.validate_config(self.working_config)
        self.setWindowTitle("Model Configuration Editor")
        self.scaling_factor = ScalingUtils.get_scaling_factor()
        base_width = 1000
        base_height = 600
        scaled_width = ScalingUtils.scale_size(base_width, self.scaling_factor)
        scaled_height = ScalingUtils.scale_size(base_height, self.scaling_factor)
        self.setGeometry(100, 100, scaled_width, scaled_height)
        self.setup_ui()

    @staticmethod
    def validate_config( config):
        model_types = ["vocal_models", "kara_models", "reverb_models", "other_models"]
        for model_type in model_types:
            if model_type not in config:
                config[model_type] = {}
        if "config_paths" not in config:
            config["config_paths"] = {}
        if "model_types" not in config:
            config["model_types"] = {}
        return config

    def setup_ui(self):
        try:
            layout = QVBoxLayout()
            self.tabs = QTabWidget()

            self.setLayout(layout)
            self.set_background_image()
            self.apply_styles()

            for model_type in ["vocal_models", "kara_models", "reverb_models", "other_models"]:
                tab = QWidget()
                tab_layout = QVBoxLayout(tab)
                table = QTableWidget()
                table.setObjectName(model_type)
                self.setup_table(table, model_type)

                add_button = QPushButton("Add New Model")
                add_button.setFixedHeight(40)
                add_button.clicked.connect(lambda checked, t=table, mt=model_type: self.add_new_model(t, mt))

                tab_layout.addWidget(table)
                tab_layout.addWidget(add_button)
                self.tabs.addTab(tab, model_type.replace("_", " ").title())

            layout.addWidget(self.tabs)

            # save cancel button
            button_widget = QWidget()
            button_layout = QHBoxLayout(button_widget)
            button_layout.setContentsMargins(0, 0, 0, 0)
            button_layout.setSpacing(10)

            self.save_button = self.create_styled_button("Save Changes")
            self.cancel_button = self.create_styled_button("Discard Changes")
            self.save_button.clicked.connect(self.save_config)
            self.cancel_button.clicked.connect(self.reject)

            button_layout.addWidget(self.save_button)
            button_layout.addWidget(self.cancel_button)

            layout.addWidget(button_widget, 0, Qt.AlignRight | Qt.AlignBottom)
            MainWindow.center_on_screen(self)

        except Exception as e:
            logger.error(f"Error in setup_ui: {str(e)}")
            logger.error(traceback.format_exc())
            QMessageBox.critical(self, "Error", f"An error occurred while setting up the UI: {str(e)}")

    def setup_table(self, table, model_type):
        try:
            table.setColumnCount(5)
            table.setHorizontalHeaderLabels(["Model Name", "Description", "Move", "Edit", "Delete"])
            table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
            table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
            table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Fixed)
            table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Fixed)
            table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Fixed)
            table.setColumnWidth(2, 80)
            table.setColumnWidth(3, 70)
            table.setColumnWidth(4, 70)
            table.verticalHeader().setDefaultSectionSize(40)

            for model, desc in self.working_config[model_type].items():
                self.add_row_to_table(table, model, desc, model_type)
        except Exception as e:
            logger.error(f"Error in setup_table for {model_type}: {str(e)}")
            logger.error(traceback.format_exc())
            QMessageBox.critical(self, "Error", f"An error occurred while setting up the table: {str(e)}")

    def add_row_to_table(self, table, model, desc, model_type):
        try:
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(model))
            desc_item = QTableWidgetItem(desc)
            desc_item.setToolTip(desc)
            table.setItem(row, 1, desc_item)

            if model != "None":
                move_widget = QWidget()
                move_layout = QHBoxLayout(move_widget)
                move_layout.setContentsMargins(0, 0, 0, 0)
                move_layout.setSpacing(2)

                up_button = self.create_tool_button("▲", "Move Up")
                down_button = self.create_tool_button("▼", "Move Down")
                up_button.clicked.connect(lambda checked, t=table, r=row, mt=model_type: self.move_row(t, r, mt, -1))
                down_button.clicked.connect(lambda checked, t=table, r=row, mt=model_type: self.move_row(t, r, mt, 1))
                move_layout.addWidget(up_button)
                move_layout.addWidget(down_button)
                table.setCellWidget(row, 2, move_widget)

                edit_button = self.create_tool_button("Edit", "Edit Model")
                edit_button.clicked.connect(lambda checked, t=table, r=row, mt=model_type: self.edit_model(t, r, mt))
                table.setCellWidget(row, 3, edit_button)

                delete_button = self.create_tool_button("Delete", "Delete Model")
                delete_button.clicked.connect(
                    lambda checked, t=table, r=row, mt=model_type: self.delete_model(t, r, mt))
                table.setCellWidget(row, 4, delete_button)
        except Exception as e:
            logger.error(f"Error in add_row_to_table: {str(e)}")
            logger.error(traceback.format_exc())
            QMessageBox.critical(self, "Error", f"An error occurred while adding a row to the table: {str(e)}")

    @staticmethod
    def create_tool_button(text, tooltip):
        button = QToolButton()
        button.setText(text)
        button.setToolTip(tooltip)
        button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        return button

    def create_styled_button(self, text):
        button = QPushButton(text)
        button_stylesheet = """
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                padding: 8px 16px;
                text-align: center;
                text-decoration: none;
                font-size: 14px;
                border-radius: 4px;
                min-width: 120px;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
            QPushButton:pressed {
                background-color: #3d8b3d;
            }
        """
        ScalingUtils.set_scaled_stylesheet(self, button_stylesheet, self.scaling_factor)
        return button

    def move_row(self, table, row, model_type, direction):
        try:
            new_row = row + direction
            if direction == -1 and new_row <= 0:
                logger.info(f"Cannot move row {row} above the first row in {model_type}")
                return
            if 0 < new_row < table.rowCount():
                # Store the data of the rows
                data_row1 = [table.item(row, col).text() if table.item(row, col) else "" for col in range(2)]
                data_row2 = [table.item(new_row, col).text() if table.item(new_row, col) else "" for col in range(2)]

                # Swap the data
                for col in range(2):
                    table.setItem(row, col, QTableWidgetItem(data_row2[col]))
                    table.setItem(new_row, col, QTableWidgetItem(data_row1[col]))

                # Update the config
                self.update_config_from_table(table, model_type)

                # Log the move operation
                logger.info(f"Moved row {row} to {new_row} in {model_type}")
        except Exception as e:
            logger.error(f"Error in move_row: {str(e)}")
            logger.error(traceback.format_exc())
            QMessageBox.critical(self, "Error", f"An error occurred while moving the row: {str(e)}")

    def update_config_from_table(self, table, model_type):
        try:
            new_order = {}
            for row in range(table.rowCount()):
                model_name = table.item(row, 0).text()
                description = table.item(row, 1).text()
                new_order[model_name] = description
            self.working_config[model_type] = new_order
            logger.info(f"Updated working config for {model_type}")
        except Exception as e:
            logger.error(f"Error in update_config_from_table: {str(e)}")
            logger.error(traceback.format_exc())
            QMessageBox.critical(self, "Error", f"An error occurred while updating the configuration: {str(e)}")

    def edit_model(self, table, row, model_type):
        try:
            model_name = table.item(row, 0).text()
            model_info = table.item(row, 1).text()
            dialog = ModelEditDialog(model_name, model_info, self.working_config, self)
            if dialog.exec_():
                updated_info = dialog.get_updated_info()
                self.working_config[model_type][model_name] = updated_info["description"]
                if "config_paths" not in self.working_config:
                    self.working_config["config_paths"] = {}
                self.working_config["config_paths"][model_name] = [updated_info["config_path"],
                                                           updated_info["fast_config_path"]]
                if "model_types" not in self.working_config:
                    self.working_config["model_types"] = {}
                self.working_config["model_types"][model_name] = updated_info["model_type"]
                if "main_tracks" not in self.working_config:
                    self.working_config["main_tracks"] = {}
                self.working_config["main_tracks"][model_name] = updated_info["main_track"]
                table.setItem(row, 1, QTableWidgetItem(updated_info["description"]))
                logger.info(f"Edited model {model_name} in {model_type}")
        except Exception as e:
            logger.error(f"Error in edit_model: {str(e)}")
            logger.error(traceback.format_exc())
            QMessageBox.critical(self, "Error", f"An error occurred while editing the model: {str(e)}")

    def delete_model(self, table, row, model_type):
        try:
            model_name = table.item(row, 0).text()
            reply = QMessageBox.question(self, 'Delete Model',
                                         f"Are you sure you want to delete the model '{model_name}'?",
                                         QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.Yes:
                del self.working_config[model_type][model_name]
                if "config_paths" in self.working_config and model_name in self.working_config["config_paths"]:
                    del self.working_config["config_paths"][model_name]
                if "model_types" in self.working_config and model_name in self.working_config["model_types"]:
                    del self.working_config["model_types"][model_name]
                table.removeRow(row)
                self.update_config_from_table(table, model_type)
                logger.info(f"Deleted model {model_name} from {model_type}")
        except Exception as e:
            logger.error(f"Error in delete_model: {str(e)}")
            logger.error(traceback.format_exc())
            QMessageBox.critical(self, "Error", f"An error occurred while deleting the model: {str(e)}")

    def add_new_model(self, table, model_type):
        try:
            model_name, ok = QInputDialog.getText(self, "Add New Model", "Enter the new model name:")
            if ok and model_name and model_name != "None":
                dialog = ModelEditDialog(model_name, "", self.working_config, self)
                if dialog.exec_():
                    updated_info = dialog.get_updated_info()
                    self.working_config[model_type][model_name] = updated_info["description"]
                    if "config_paths" not in self.working_config:
                        self.working_config["config_paths"] = {}
                    self.working_config["config_paths"][model_name] = [updated_info["config_path"], updated_info["fast_config_path"]]
                    if "model_types" not in self.working_config:
                        self.working_config["model_types"] = {}
                    self.working_config["model_types"][model_name] = updated_info["model_type"]
                    if "main_tracks" not in self.working_config:
                        self.working_config["main_tracks"] = {}
                    self.working_config["main_tracks"][model_name] = updated_info["main_track"]
                    self.add_row_to_table(table, model_name, updated_info["description"], model_type)
                    self.update_config_from_table(table, model_type)
                    logger.info(f"Added new model {model_name} to {model_type}")
            elif model_name == "None":
                QMessageBox.warning(self, "Invalid Model Name", "You cannot add a model named 'None'.")
        except Exception as e:
            logger.error(f"Error in add_new_model: {str(e)}")
            logger.error(traceback.format_exc())
            QMessageBox.critical(self, "Error", f"An error occurred while adding a new model: {str(e)}")

    def save_config(self):
        try:
            self.original_config.clear()
            self.original_config.update(copy.deepcopy(self.working_config))
            with open(CONFIG_FILE, 'w') as f:
                json.dump(self.original_config, f, ensure_ascii=False, indent=4)
            logger.info("Configuration saved successfully")
            self.accept()
        except Exception as e:
            logger.error(f"Error in save_config: {str(e)}")
            logger.error(traceback.format_exc())
            QMessageBox.critical(self, "Error", f"Failed to save configuration: {str(e)}")

    def set_background_image(self):
        background = QPixmap(":/images/background2.png")
        if background.isNull():
            print("Failed to load background image")
            return

        overlay = QPixmap(background.size())
        overlay.fill(QColor(255, 255, 255, 128))

        painter = QPainter(background)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        painter.drawPixmap(0, 0, overlay)
        painter.end()

        self.background_label = QLabel(self)
        self.background_label.setPixmap(background)
        self.background_label.setScaledContents(True)
        self.background_label.resize(self.size())
        self.background_label.lower()
        self.setAttribute(Qt.WA_StyledBackground, True)

    def apply_styles(self):
        config_editor_dialog_stylesheet = """
            QDialog { background-color: #ffffff; }
            QTabWidget::tab-bar { left: 5px; }
            QTabWidget::pane {
            border: 1px solid #cccccc;
            background-color: rgba(255, 255, 255, 200);
            }
            QTabBar::tab {
                background-color: #f0f0f0;
                border: 1px solid #cccccc;
                padding: 5px 10px;
                margin-right: 2px;
                border-top-left-radius: 5px;
                border-top-right-radius: 5px;
            }
            QTabBar::tab:selected {
                background-color: #ffffff;
                border-bottom-color: #ffffff;
            }
            QTableWidget {
                background-color: rgba(255, 255, 255, 100);
                border: 1px solid #cccccc;
                gridline-color: #ececec;
            }
            QTableWidget::item { padding: 5px; }
            QHeaderView::section {
                background-color: #d0f0c0;
                padding: 5px;
                border: 1px solid #cccccc;
                font-weight: bold;
            }
            QPushButton, QToolButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                padding: 5px 10px;
                text-align: center;
                text-decoration: none;
                font-size: 12px;
                border-radius: 3px;
            }
            QPushButton:hover, QToolButton:hover { background-color: #45a049; }
        """
        ScalingUtils.set_scaled_stylesheet(self, config_editor_dialog_stylesheet, self.scaling_factor)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.background_label = None
        self.inference_thread = None
        self.setWindowTitle("MSST GUI v1.4     by 领航员未鸟")
        self.scaling_factor = ScalingUtils.get_scaling_factor()
        logger.info(f"Detected scaling factor: {self.scaling_factor}")
        base_width = 800
        base_height = 810
        scaled_width = ScalingUtils.scale_size(base_width, self.scaling_factor)
        scaled_height = ScalingUtils.scale_size(base_height, self.scaling_factor)
        self.setGeometry(100, 100, scaled_width, scaled_height)
        self.base_font = QApplication.font()
        scaled_font = ScalingUtils.scale_font(self.base_font, self.scaling_factor)
        QApplication.setFont(scaled_font)
        logger.info(f"Applied font scaling: {self.base_font.pointSize()}pt -> {scaled_font.pointSize()}pt")
        self.setWindowIcon(QIcon(":/images/msst-icon.ico"))

        if sys.platform == 'win32':
            import ctypes
            myappid = 'AliceNavigator.MSST-GUI.v1.4'
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)

        self.config = load_or_create_config()

        # Create SystemInfoThread and connect its signals
        self.system_info_thread = SystemInfoThread()
        self.system_info_thread.info_signal.connect(self.update_output)
        QTimer.singleShot(100, self.print_system_info)

        # Set default input folder
        self.input_folder = os.path.join(os.getcwd(), 'input')
        if not os.path.exists(self.input_folder):
            os.makedirs(self.input_folder)

        if not os.path.exists('pretrain'):
            os.makedirs('pretrain')

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        # Preset management
        preset_layout = QHBoxLayout()
        self.preset_combo = CustomComboBox()
        self.load_presets()
        preset_layout.addWidget(QLabel("Preset:"))
        preset_layout.addWidget(self.preset_combo, 1)
        self.preset_combo.currentIndexChanged.connect(self.load_preset_from_combo)

        self.save_preset_button = QPushButton("Save Preset")
        self.save_preset_button.clicked.connect(self.save_preset)
        preset_layout.addWidget(self.save_preset_button)
        main_layout.addLayout(preset_layout)

        # Model selection
        model_group = QGroupBox("Model Selection")
        model_layout = QVBoxLayout()
        model_group.setLayout(model_layout)

        self.vocal_model_combo = self.create_model_combo(self.config["vocal_models"])
        self.vocal_model_tooltip = self.create_tooltip_label()
        model_layout.addLayout(
            self.create_model_section("Vocal Model:", self.vocal_model_combo, self.vocal_model_tooltip))

        self.kara_model_combo = self.create_model_combo(self.config["kara_models"])
        self.kara_model_tooltip = self.create_tooltip_label()
        model_layout.addLayout(
            self.create_model_section("Karaoke Model:", self.kara_model_combo, self.kara_model_tooltip))

        self.reverb_model_combo = self.create_model_combo(self.config["reverb_models"])
        self.reverb_model_tooltip = self.create_tooltip_label()
        model_layout.addLayout(
            self.create_model_section("Reverb Model:", self.reverb_model_combo, self.reverb_model_tooltip))

        self.other_model_combo = self.create_model_combo(self.config["other_models"])
        self.other_model_tooltip = self.create_tooltip_label()
        model_layout.addLayout(
            self.create_model_section("Other Model:", self.other_model_combo, self.other_model_tooltip))

        self.update_model_combos()
        main_layout.addWidget(model_group)

        # Inference options and inference env input
        options_layout = QHBoxLayout()
        options_layout.setSpacing(20)  # Increase overall spacing between main elements

        left_options = QHBoxLayout()
        left_options.setSpacing(78)  # Increase spacing between checkboxes

        self.fast_inference_checkbox = QCheckBox("Fast Inference Mode")
        self.fast_inference_checkbox.setToolTip(
            "Use lower inference parameters for 3-4x speed boost at slightly lower SDR")
        left_options.addWidget(self.fast_inference_checkbox)

        self.force_cpu_checkbox = QCheckBox("Force CPU")
        self.force_cpu_checkbox.setToolTip("Force the use of CPU even if CUDA is available")
        left_options.addWidget(self.force_cpu_checkbox)

        self.use_tta_checkbox = QCheckBox("Use TTA")
        self.use_tta_checkbox.setToolTip("Use test time augmentation(TTA). While this triples the runtime, it reduces noise and slightly improves prediction quality.")
        left_options.addWidget(self.use_tta_checkbox)

        options_layout.addLayout(left_options)

        options_layout.addStretch(1)  # Add stretch to push the following elements to the right

        right_options = QHBoxLayout()
        right_options.setSpacing(5)  # Kept the reduced spacing between elements on the right

        env_input_label = QLabel("Inference Env:")
        right_options.addWidget(env_input_label)

        self.inference_env_input = QLineEdit()
        self.inference_env_input.setPlaceholderText("Path to Python executable")
        self.inference_env_input.setText(r'.\env\python.exe')
        self.inference_env_input.setToolTip("Path to Python executable for inference")
        self.inference_env_input.setFixedWidth(200)  # Kept the adjusted width
        right_options.addWidget(self.inference_env_input)

        self.browse_button = QToolButton()
        self.browse_button.setIcon(QIcon(":/images/hammer-and-anvil.png"))
        self.browse_button.setIconSize(QSize(24, 24))
        self.browse_button.setToolTip("Browse for Python executable")
        self.browse_button.clicked.connect(self.browse_inference_env)
        right_options.addWidget(self.browse_button)

        options_layout.addLayout(right_options)

        main_layout.addLayout(options_layout)

        self.inference_env_input.setObjectName("inference_env_input")
        self.browse_button.setObjectName("browse_button")

        # Connect the textChanged signal to save the inference env
        self.inference_env_input.setText(self.config.get("inference_env", r'.\env\python.exe'))
        self.inference_env_input.textChanged.connect(self.save_inference_env)

        # Input folder selection
        folder_layout = QHBoxLayout()
        self.input_folder_button = QPushButton("Select Input Folder")
        self.input_folder_button.clicked.connect(self.select_input_folder)
        folder_layout.addWidget(self.input_folder_button)

        self.input_folder_display = QLineEdit(self.input_folder)
        self.input_folder_display.setReadOnly(True)
        self.input_folder_display.setToolTip(self.input_folder)
        folder_layout.addWidget(self.input_folder_display, 1)

        main_layout.addLayout(folder_layout)

        # button to open the input folder
        self.open_folder_button = QToolButton()
        self.open_folder_button.setIcon(QIcon(":/images/folder-open.png"))
        self.open_folder_button.setIconSize(QSize(24, 24))
        self.open_folder_button.setToolTip("Open Input Folder")
        self.open_folder_button.clicked.connect(self.open_input_folder)
        open_folder_button_stylesheet = """
            QToolButton {
                padding-left: 1px;
            }
        """
        ScalingUtils.set_scaled_stylesheet(self, open_folder_button_stylesheet, self.scaling_factor)
        folder_layout.addWidget(self.open_folder_button)

        # button to open the archive folder
        self.open_archive_button = QToolButton()
        self.open_archive_button.setIcon(QIcon(":/images/document-folder"))
        self.open_archive_button.setIconSize(QSize(24, 24))
        self.open_archive_button.setToolTip("Open Archive Folder")
        self.open_archive_button.clicked.connect(self.open_archive_folder)
        open_archive_button_stylesheet = """
                            QToolButton {
                                padding-left: 1px;
                            }
                        """
        ScalingUtils.set_scaled_stylesheet(self, open_archive_button_stylesheet, self.scaling_factor)
        folder_layout.addWidget(self.open_archive_button)

        main_layout.addLayout(folder_layout)

        # Action buttons
        action_layout = QHBoxLayout()
        self.run_button = QPushButton("Run Inference")
        self.run_button.clicked.connect(self.run_inference)
        action_layout.addWidget(self.run_button)
        self.archive_button = QPushButton("Archive Results")
        self.archive_button.clicked.connect(self.run_archive)
        action_layout.addWidget(self.archive_button)

        # Add Config Editor button
        self.config_editor_button = QPushButton("Edit Model Config")
        self.config_editor_button.clicked.connect(self.open_config_editor)
        action_layout.addWidget(self.config_editor_button)

        main_layout.addLayout(action_layout)

        # Output console
        self.output_console = QTextEdit()
        self.output_console.setReadOnly(True)
        console_stylesheet = """
            QTextEdit {
                background-color: #1e1e1e;
                color: #ffffff;
                font-family: 'Courier New', monospace;
                font-size: 12px;
                border: 1px solid #3a3a3a;
            }
        """
        ScalingUtils.set_scaled_stylesheet(self, console_stylesheet, self.scaling_factor)
        self.set_background_image()
        font = QFont("Microsoft YaHei Mono", ScalingUtils.scale_size(10, self.scaling_factor))
        if not QFontInfo(font).fixedPitch():
            font = QFont("Noto Sans Mono CJK SC", ScalingUtils.scale_size(10, self.scaling_factor))

        if not QFontInfo(font).fixedPitch():
            # use sys default font if cant find any
            font = QFont()
            font.setStyleHint(QFont.Monospace)
            font.setFixedPitch(True)

        self.output_console.setFont(font)
        main_layout.addWidget(self.output_console)

        # Connect signals
        self.vocal_model_combo.currentIndexChanged.connect(
            lambda: self.update_tooltip(self.vocal_model_combo, self.vocal_model_tooltip))
        self.kara_model_combo.currentIndexChanged.connect(
            lambda: self.update_tooltip(self.kara_model_combo, self.kara_model_tooltip))
        self.reverb_model_combo.currentIndexChanged.connect(
            lambda: self.update_tooltip(self.reverb_model_combo, self.reverb_model_tooltip))
        self.other_model_combo.currentIndexChanged.connect(
            lambda: self.update_tooltip(self.other_model_combo, self.other_model_tooltip))

        # Initial tooltip update
        self.update_tooltip(self.vocal_model_combo, self.vocal_model_tooltip)
        self.update_tooltip(self.kara_model_combo, self.kara_model_tooltip)
        self.update_tooltip(self.reverb_model_combo, self.reverb_model_tooltip)
        self.update_tooltip(self.other_model_combo, self.other_model_tooltip)

        # Set styles
        main_stylesheet = """
            QMainWindow {
                background-color: #f0f0f0;
            }
            QGroupBox {
                font-weight: bold;
                border: 1px solid #cccccc;
                border-radius: 5px;
                margin-top: 1ex;
                padding: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 3px 0 3px;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                padding: 8px 16px;
                text-align: center;
                text-decoration: none;
                font-size: 14px;
                margin: 4px 2px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
            QPushButton:pressed {
                background-color: #3e8e41;
                padding-top: 10px;
                padding-bottom: 6px;
            }
            QComboBox {
                padding: 5px;
                border: 1px solid #cccccc;
                border-radius: 3px;
                background-color: white;
                min-width: 6em;
                font-size: 10pt;
            }
            QComboBox::drop-down {
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 20px;
                border-left-width: 1px;
                border-left-color: #cccccc;
                border-left-style: solid;
                border-top-right-radius: 3px;
                border-bottom-right-radius: 3px;
                font-size: 10pt;
            }
            QComboBox::down-arrow {
                width: 14px;
                height: 14px;
            }
            QComboBox::down-arrow:on {
                top: 1px;
                left: 1px;
            }
            QCheckBox {
                spacing: 5px;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border: 1px solid #cccccc;
                border-radius: 3px;
            }
            QCheckBox::indicator:unchecked {
                background-color: white;
            }
            QCheckBox::indicator:checked {
                background-color: #4CAF50;
            }
            QTextEdit {
            background-color: rgba(30, 30, 30, 220);
            }
            QGroupBox, QComboBox, QLineEdit {
            background-color: rgba(255, 255, 255, 220);
            }
            QLineEdit {
                padding: 2px 5px;
                border: 1px solid #cccccc;
                border-radius: 3px;
                background-color: white;
                font-size: 12px;
            }
            QLineEdit:focus {
                border-color: #4CAF50;
            }
            QToolButton {
                border: 1px solid #cccccc;
                border-radius: 3px;
            }
            QToolButton:hover {
                border: 1px solid #cccccc;
                border-radius: 3px;
                background-color: #e0e0e0;
            }
            QToolButton:pressed {
                background-color: #d0d0d0;
            }
            QLabel {
                font-size: 12px;
            }
            QCheckBox {
                font-size: 12px;
            }
        """
        ScalingUtils.set_scaled_stylesheet(self, main_stylesheet, self.scaling_factor)
        self.center_on_screen()
        logger.info("MainWindow initialization completed")

    def check_inference_env(self):
        inference_env = self.inference_env_input.text().strip()
        if inference_env.lower() == 'python':
            # If set to use system Python, check if it's available
            try:
                subprocess.run(['python', '--version'], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                logger.info("Using system Python for inference")
            except subprocess.CalledProcessError:
                logger.error("System Python not found")
                QMessageBox.warning(self, "Error", "System Python not found. Please install Python or set the correct path in the configuration.")
                return False
        elif not os.path.exists(inference_env):
            logger.error(f"Inference environment not found: {inference_env}")
            QMessageBox.warning(self, "Error", f"Inference environment not found: {inference_env}\nPlease check your configuration and ensure the path is correct.")
            return False
        return True

    def save_inference_env(self):
        inference_env = self.inference_env_input.text().strip()
        self.config["inference_env"] = inference_env
        self.save_env_config()
        logger.info(f"Saved inference env: {inference_env}")

    def save_env_config(self):
        with open(CONFIG_FILE, 'w') as f:
            json.dump(self.config, f, ensure_ascii=False, indent=4)
        logger.info("Inference env configuration saved")

    @staticmethod
    def create_model_combo(options):
        combo = CustomComboBox()
        combo.addItems(options.keys())
        for i, (option, tooltip) in enumerate(options.items()):
            combo.setItemData(i, tooltip, Qt.ToolTipRole)
        return combo

    def create_tooltip_label(self):
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setMaximumHeight(ScalingUtils.scale_size(25, self.scaling_factor))
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        label = QLabel()
        label.setWordWrap(True)
        label_stylesheet = """
            QLabel {
                background-color: #d0f0c0;
                border: 1px solid #cccccc;
                border-radius: 5px;
                padding: 5px;
                font-size: 11px;
                color: #333333;
            }
        """
        ScalingUtils.set_scaled_stylesheet(self, label_stylesheet, self.scaling_factor)

        scroll_area.setWidget(label)
        scroll_area.setStyleSheet("""
            QScrollArea {
                border: none;
                background-color: transparent;
            }
        """)

        return scroll_area

    def create_model_section(self, label_text, combo, tooltip_label):
        layout = QVBoxLayout()
        label = QLabel(label_text)
        label_stylesheet = """font-size: 10pt;"""
        ScalingUtils.set_scaled_stylesheet(self, label_stylesheet, self.scaling_factor)
        layout.addWidget(label)
        layout.addWidget(combo)
        layout.addWidget(tooltip_label)
        layout.addSpacing(ScalingUtils.scale_size(10, self.scaling_factor))  # Add some space between sections
        return layout

    def browse_inference_env(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Select Python Executable", "", "Python Executable (*.exe);;All Files (*)")
        if file_path:
            self.inference_env_input.setText(file_path)
            self.save_inference_env()

    @staticmethod
    def update_tooltip(combo, tooltip_scroll_area):
        current_index = combo.currentIndex()
        tooltip = combo.itemData(current_index, Qt.ToolTipRole)
        label = tooltip_scroll_area.widget()
        label.setText(tooltip)
        tooltip_scroll_area.setVisible(bool(tooltip))

    def set_background_image(self):
        background = QPixmap(":/images/background3.png")
        if background.isNull():
            print("Failed to load background image")
            return

        overlay = QPixmap(background.size())
        overlay.fill(QColor(255, 255, 255, 128))

        painter = QPainter(background)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        painter.drawPixmap(0, 0, overlay)
        painter.end()

        self.background_label = QLabel(self)
        self.background_label.setPixmap(background)
        self.background_label.setScaledContents(True)
        self.background_label.resize(self.size())
        self.background_label.lower()
        self.setAttribute(Qt.WA_StyledBackground, True)

    def select_input_folder(self):
        logger.info("Selecting input folder")
        folder = QFileDialog.getExistingDirectory(self, "Select Input Folder", self.input_folder)
        if folder:
            self.input_folder = folder
            self.update_input_folder_display()
            logger.info(f"Selected input folder: {folder}")

    def update_input_folder_display(self):
        self.input_folder_display.setText(self.input_folder)
        self.input_folder_display.setToolTip(self.input_folder)
        self.input_folder_display.setCursorPosition(0)

    def open_input_folder(self):
        if os.path.exists(self.input_folder):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.input_folder))
        else:
            QMessageBox.warning(self, "Error", "Input folder does not exist.")

    def open_archive_folder(self):
        archive_folder = os.path.join(os.getcwd(), 'archive')
        if os.path.exists(archive_folder):
            QDesktopServices.openUrl(QUrl.fromLocalFile(archive_folder))
        else:
            QMessageBox.warning(self, "Error", "Archive folder does not exist.")

    def load_presets(self):
        logger.info("Loading presets")
        if not os.path.exists('presets'):
            os.makedirs('presets')
            logger.info("Created presets directory")
        preset_files = [f for f in os.listdir('presets') if f.endswith('.json')]
        current_preset = self.preset_combo.currentText()
        self.preset_combo.clear()
        self.preset_combo.addItem("Select a preset")  # Default option
        preset_names = [os.path.splitext(f)[0] for f in preset_files]
        self.preset_combo.addItems(preset_names)
        index = self.preset_combo.findText(current_preset)
        if index >= 0:
            self.preset_combo.setCurrentIndex(index)
        logger.debug(f"Loaded presets: {preset_names}")

    def load_preset_from_combo(self, index):
        preset_name = self.preset_combo.itemText(index)
        if preset_name and preset_name != "Select a preset":
            self.load_preset(preset_name)

    @staticmethod
    def convert_none_to_false(value):
        return False if value == "None" else value

    @staticmethod
    def convert_false_to_none(value):
        return "None" if value is False else value

    def load_preset(self, preset_name):
        if not preset_name:
            logger.warning("Attempted to load preset with empty name")
            return

        logger.info(f"Loading preset: {preset_name}")
        try:
            with open(f"presets/{preset_name}.json", "r") as f:
                preset = json.load(f)
            self.vocal_model_combo.setCurrentText(self.convert_false_to_none(preset["vocal_model_name"]))
            self.kara_model_combo.setCurrentText(self.convert_false_to_none(preset["kara_model_name"]))
            self.reverb_model_combo.setCurrentText(self.convert_false_to_none(preset["reverb_model_name"]))
            self.other_model_combo.setCurrentText(self.convert_false_to_none(preset["other_model_name"]))
            self.fast_inference_checkbox.setChecked(preset.get("if_fast", True))
            self.force_cpu_checkbox.setChecked(preset.get("force_cpu", False))
            self.use_tta_checkbox.setChecked(preset.get("use_tta", False))
            logger.info(f"Preset loaded successfully: {preset_name}")
        except FileNotFoundError:
            logger.error(f"Preset file not found: {preset_name}")
            QMessageBox.warning(self, "Error", f"Preset file '{preset_name}' not found.")
        except Exception as e:
            logger.error(f"Failed to load preset: {str(e)}", exc_info=True)
            QMessageBox.warning(self, "Error", f"Failed to load preset: {str(e)}")

    def save_preset(self):
        logger.info("Saving preset")
        name, ok = QInputDialog.getText(self, "Save Preset", "Enter preset name:")
        if ok:
            if name.strip():
                preset = {
                    "vocal_model_name": self.convert_none_to_false(self.vocal_model_combo.currentText()),
                    "kara_model_name": self.convert_none_to_false(self.kara_model_combo.currentText()),
                    "reverb_model_name": self.convert_none_to_false(self.reverb_model_combo.currentText()),
                    "other_model_name": self.convert_none_to_false(self.other_model_combo.currentText()),
                    "if_fast": self.fast_inference_checkbox.isChecked(),
                    "force_cpu": self.force_cpu_checkbox.isChecked(),
                    "use_tta": self.use_tta_checkbox.isChecked(),
                    "preset_name": name
                }
                if not os.path.exists('presets'):
                    os.makedirs('presets')
                    logger.info("Created presets directory")
                with open(f"presets/{name}.json", "w") as f:
                    json.dump(preset, f, ensure_ascii=False, indent=4)
                self.load_presets()
                index = self.preset_combo.findText(name)
                if index >= 0:
                    self.preset_combo.setCurrentIndex(index)
                QMessageBox.information(self, "Preset Saved", f"Preset '{name}' has been saved.")
                logger.info(f"Preset saved: {name}")
            else:
                logger.warning("Empty preset name provided")
                QMessageBox.warning(self, "Save Failed", "Preset was not saved due to an empty name.")
        else:
            logger.info("Preset save cancelled")

    def get_config_path(self, model, is_fast):
        config_paths = self.config["config_paths"].get(model, [None, None])
        return config_paths[1] if is_fast else config_paths[0]

    def get_model_type(self, model):
        return self.config["model_types"].get(model, "unknown")

    @staticmethod
    def safe_path(path):
        path = os.path.normpath(path)
        path = path.replace('"', '\\"')
        return f'"{path}"'

    def run_inference(self):
        logger.info("Starting inference process")
        inference_env = self.inference_env_input.text().strip()

        # Check inference environment before proceeding
        if not self.check_inference_env():
            return

        if not os.path.exists(self.input_folder):
            logger.warning("Input folder does not exist")
            QMessageBox.warning(self, "Error", "Input folder does not exist. Please select a valid folder.")
            return

        if not os.listdir(self.input_folder):
            QMessageBox.warning(self, "No Files",
                                "The input folder is empty. Please add some audio files and try again.")
            return

        is_valid, missing_items = self.validate_selected_models()
        if not is_valid:
            logger.error(f"Model validation failed with {len(missing_items)} missing items")
            QApplication.beep()
            organized_missing = self.organize_missing_items(missing_items)
            dialog = QDialog(self)
            dialog.setWindowTitle("Model validation failed")
            dialog.setModal(True)
            dialog.setMinimumSize(600, 400)
            layout = QVBoxLayout(dialog)
            label = QLabel("The following models or configuration files are missing. Please download and place them in the correct location, then try again:")
            label.setWordWrap(True)
            layout.addWidget(label)
            text_edit = QTextEdit()
            text_edit.setReadOnly(True)
            text_edit.setPlainText(organized_missing)
            text_edit.setStyleSheet("""
                    QTextEdit {
                        background-color: #f0f4f8;
                        border: 1px solid #dee2e6;
                        border-radius: 4px;
                        padding: 8px;
                    }
                """)
            layout.addWidget(text_edit)
            button_box = QDialogButtonBox(QDialogButtonBox.Ok)
            button_box.accepted.connect(dialog.accept)
            layout.addWidget(button_box)
            dialog.exec_()
            return

        # Inference
        inference_base = inference_env + ' inference.py'
        fast_inference = self.fast_inference_checkbox.isChecked()
        force_cpu = self.force_cpu_checkbox.isChecked()
        use_tta = self.use_tta_checkbox.isChecked()
        commands = []
        current_input_folder = self.safe_path(self.input_folder)

        def add_command(model, store_dir):
            nonlocal current_input_folder
            if model != "None":
                config_path = self.safe_path(self.get_config_path(model, fast_inference))
                model_type = self.get_model_type(model)
                model_path = self.safe_path(f"pretrain/{model}")
                if config_path and model_type != "unknown":
                    cmd = f"{inference_base} --model_type {model_type} --start_check_point {model_path} --input_folder {current_input_folder} --store_dir {store_dir} --extract_instrumental --config_path {config_path}"
                    if force_cpu:
                        cmd += " --force_cpu"
                    if use_tta:
                        cmd += " --use_tta"
                    commands.append((cmd, store_dir))
                    current_input_folder = store_dir
                    logger.info(f"Added command for {model}: {cmd}")
                else:
                    logger.warning(f"No config file or unknown model type for model: {model}")

        add_command(self.vocal_model_combo.currentText(), "separation_results")
        add_command(self.kara_model_combo.currentText(), "karaoke_results")
        add_command(self.reverb_model_combo.currentText(), "deverb_results")
        add_command(self.other_model_combo.currentText(), "other_results")

        logger.info(f"Inference commands: {commands}")

        self.output_console.clear()
        self.update_output("Starting inference...", color='cyan')
        # self.print_separator()
        self.inference_thread = InferenceThread(commands, self.input_folder)
        self.inference_thread.update_signal.connect(self.process_inference_output)
        self.inference_thread.finished_signal.connect(self.inference_finished)
        self.inference_thread.file_organization_signal.connect(self.file_organization_completed)
        self.inference_thread.start()

        self.run_button.setText("Stop Inference")
        run_button_stylesheet = """
            QPushButton {
                background-color: #FF4136;
                color: white;
                border: none;
                padding: 8px 16px;
                text-align: center;
                text-decoration: none;
                font-size: 14px;
                margin: 4px 2px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #E02F26;
            }
            QPushButton:pressed {
                background-color: #C7291F;
                padding-top: 10px;
                padding-bottom: 6px;
            }
        """
        self.run_button.setStyleSheet(ScalingUtils.scale_stylesheet(run_button_stylesheet, self.scaling_factor))
        self.run_button.clicked.disconnect()
        self.run_button.clicked.connect(self.stop_inference)

    def file_organization_completed(self, moved_files, time_taken):
        self.update_output(f"Organized {moved_files} instrumental files in {time_taken:.2f} seconds", color='green')

    def stop_inference(self):
        if hasattr(self, 'inference_thread') and self.inference_thread.isRunning():
            logger.info("Stopping inference process")
            self.inference_thread.stop()
            self.update_output("\nStopping inference process. Please wait...", color='yellow')
            self.inference_thread.wait()
            self.update_output("Inference process stopped by user.", color='yellow')
            self.reset_run_button()

    def reset_run_button(self):
        self.run_button.setText("Run Inference")
        self.run_button.setStyleSheet("")  # This will reset to the default style defined in setStyleSheet
        self.run_button.clicked.disconnect()
        self.run_button.clicked.connect(self.run_inference)

    def process_inference_output(self, text, is_progress_update):
        cursor = self.output_console.textCursor()
        cursor.movePosition(QTextCursor.End)

        if is_progress_update:
            cursor.movePosition(QTextCursor.StartOfLine, QTextCursor.KeepAnchor)
            cursor.removeSelectedText()
            self.update_output(text, color='#ffa500', auto_newline=False)
        else:
            if text.startswith("Module:"):
                self.print_separator(char='=')
                self.update_output(text, color='#6e71ff')
            elif text.startswith("Command:"):
                self.update_output(text, color='yellow', italic=True)
                self.print_separator(char='-')
            elif "error" in text.lower():
                self.update_output(text, color='red')
            elif "warning" in text.lower():
                self.update_output(text, color='orange')
            else:
                self.update_output(text)

        self.output_console.setTextCursor(cursor)
        self.output_console.ensureCursorVisible()

    def inference_finished(self, summary):
        self.reset_run_button()
        self.print_separator(char='=')
        self.update_output("Inference process completed!", color='green', bold=True)
        self.print_separator(char='=')
        self.update_output("Summary:", color='#6e71ff', bold=True)
        self.update_output(f"Total files processed: {summary['total_files']}", color='#6e71ff')
        self.update_output(f"Total processing time: {summary['total_time']:.2f} seconds", color='#6e71ff')
        self.update_output("Modules processed:", color='#6e71ff')
        for module_name, store_dir in summary['modules']:
            self.update_output(f"- {module_name}: {store_dir} ", color='#6e71ff')
        if summary['errors'] > 0:
            self.update_output(f"Errors encountered: {summary['errors']}", color='red')
        # self.print_separator(char='=')
        logger.info(f"Inference summary displayed. Total files: {summary['total_files']}")

    def update_output(self, text, color='white', bold=False, italic=False, auto_newline=True):
        cursor = self.output_console.textCursor()
        format = QTextCharFormat()
        format.setForeground(QColor(color))
        if bold:
            format.setFontWeight(QFont.Bold)
        if italic:
            format.setFontItalic(True)
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(text + ('\n' if auto_newline else ''), format)
        self.output_console.setTextCursor(cursor)
        self.output_console.ensureCursorVisible()

    def print_separator(self, char='-'):
        self.update_output(char * 80, color='gray')

    def validate_selected_models(self):
        missing_items = []
        selected_models = []
        model_categories = [
            ("Vocal Model", self.vocal_model_combo.currentText()),
            ("Karaoke Model", self.kara_model_combo.currentText()),
            ("Reverb Model", self.reverb_model_combo.currentText()),
            ("Other Model", self.other_model_combo.currentText())
        ]

        for category_name, model_name in model_categories:
            if model_name != "None":
                selected_models.append((category_name, model_name))
        if not selected_models:
            return True, []

        fast_inference = self.fast_inference_checkbox.isChecked()
        for module_name, model in selected_models:
            model_path = os.path.join("pretrain", model)
            if not os.path.exists(model_path):
                missing_items.append(f"{module_name} - Missing model file: {model}")
            config_paths = self.config["config_paths"].get(model, [None, None])
            config_path = config_paths[1] if fast_inference else config_paths[0]
            if not config_path:
                config_type = "Fast" if fast_inference else "Standard"
                missing_items.append(f"{module_name} - Missing {config_type} config file path")
            elif not os.path.exists(config_path):
                config_type = "Fast" if fast_inference else "Standard"
                missing_items.append(f"{module_name} - Missing {config_type} config file: {os.path.basename(config_path)}")
        return len(missing_items) == 0, missing_items

    @staticmethod
    def organize_missing_items(missing_items):
        organized = {}
        for item in missing_items:
            if " - " in item:
                module_part, detail_part = item.split(" - ", 1)
                if module_part not in organized:
                    organized[module_part] = []
                organized[module_part].append(detail_part)
            else:
                if "Other" not in organized:
                    organized["Other"] = []
                organized["Other"].append(item)
        output = []
        for module, items in organized.items():
            output.append(f"【{module}】")
            for item in items:
                output.append(f"  • {item}")
            output.append("")
        output.append("[Steps]")
        output.append("1. Please download the above missing models and their configuration files.")
        output.append("2. Place the models in the 'pretrain' folder and the configuration files in the 'configs' folder.")
        output.append("3. Run the inference again.")
        return "\n".join(output)

    def run_archive(self):
        logger.info("Starting archive process")

        self.output_console.clear()
        self.update_output("Starting archive process...", color='green')
        self.print_separator()

        def archive_output_callback(text):
            self.update_output(text, color='#6e71ff')

        try:
            archive_folders(output_callback=archive_output_callback)

            logger.info("Archive process completed")
            self.print_separator()
            self.update_output("Archive process completed.", color='green')
            self.print_separator()
        except Exception as e:
            error_msg = f"Error occurred during archiving: {str(e)}"
            logger.error(f"Archive process failed: {error_msg}")
            self.update_output("An error occurred during archiving!", color='red')
            self.update_output(error_msg, color='red')
            self.print_separator()
            QMessageBox.critical(
                self,
                "Archiving Error",
                f"An error occurred during archiving:\n\n{str(e)}\n\nPlease check if the files are being used by another program and try again."
            )

    def open_config_editor(self):
        dialog = ConfigEditorDialog(self.config, self)
        if dialog.exec_():
            self.config = load_or_create_config()  # Reload the config from file
            self.update_model_combos()

    def update_model_combos(self):
        self.update_single_combo(self.vocal_model_combo, self.vocal_model_tooltip, self.config["vocal_models"])
        self.update_single_combo(self.kara_model_combo, self.kara_model_tooltip, self.config["kara_models"])
        self.update_single_combo(self.reverb_model_combo, self.reverb_model_tooltip, self.config["reverb_models"])
        self.update_single_combo(self.other_model_combo, self.other_model_tooltip, self.config["other_models"])

    def update_single_combo(self, combo, tooltip_label, options):
        current_text = combo.currentText()
        combo.clear()
        combo.addItems(options.keys())
        for i, (option, tooltip) in enumerate(options.items()):
            combo.setItemData(i, tooltip, Qt.ToolTipRole)
        index = combo.findText(current_text)
        if index >= 0:
            combo.setCurrentIndex(index)
        self.update_tooltip(combo, tooltip_label)

    def print_system_info(self):
        self.output_console.clear()
        self.system_info_thread.start()

    def print_with_delay(self, text, color='white', bold=False, italic=False, auto_newline=True, delay=10):
        QMetaObject.invokeMethod(self.system_info_thread, 'print_with_delay',
                                 Qt.QueuedConnection,
                                 Q_ARG(str, text),
                                 Q_ARG(str, color),
                                 Q_ARG(bool, bold),
                                 Q_ARG(bool, italic),
                                 Q_ARG(bool, auto_newline),
                                 Q_ARG(int, delay))

    def center_on_screen(self):
        screen_geometry = QApplication.primaryScreen().geometry()
        x = (screen_geometry.width() - self.width()) // 2
        y = (screen_geometry.height() - self.height()) // 2
        self.move(x, y)


if __name__ == "__main__":
    logger.info("Starting application")
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    remove_screen_splash()
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())

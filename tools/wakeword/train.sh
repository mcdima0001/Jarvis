#!/usr/bin/env bash
#
# Обучение модели активации «Джарвис». Запускать на Linux или в WSL2 с
# видеокартой NVIDIA — генератор примеров под Windows не работает, это
# ограничение самого openWakeWord, а не наше.
#
#   ./train.sh trial   — пробный прогон: 500 примеров, 2000 шагов, ~20 минут.
#                        Нужен, чтобы вся цепочка споткнулась сегодня, а не на
#                        восьмом часу настоящего обучения.
#   ./train.sh         — настоящий прогон по jarvis.yml.
#
# Скрипт можно прерывать и запускать заново: скачанное докачивается (`wget -c`),
# уже сгенерированные примеры пересчитываются только если их меньше нужного.

set -euo pipefail
cd "$(dirname "$0")"

MODE="${1:-full}"
WORK="${WORK:-$PWD/work}"
FEATURES="openwakeword_features_ACAV100M_2000_hrs_16bit.npy"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mОшибка: %s\033[0m\n' "$*" >&2; exit 1; }

# --- проверки до того, как что-то качать ------------------------------------

say "Проверяю, на чём предстоит считать"
command -v python3 >/dev/null || die "нет python3"
command -v wget >/dev/null || die "нет wget (apt install wget)"
command -v git >/dev/null || die "нет git (apt install git)"

if command -v nvidia-smi >/dev/null && nvidia-smi >/dev/null 2>&1; then
    nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
else
    cat <<'WARN'
Видеокарты не видно. Обучиться можно и на процессоре, но генерация примеров
займёт не часы, а сутки. Если это WSL — проверь, что стоит свежий драйвер
NVIDIA в самой Windows (внутрь WSL драйвер не ставят) и что `nvidia-smi`
отвечает.
WARN
    read -r -p "Продолжить всё равно? [y/N] " answer
    [ "$answer" = "y" ] || exit 1
fi

# 17 ГБ признаков плюс примеры плюс модели — с запасом нужно около сорока.
free_gb=$(df -BG --output=avail "$PWD" | tail -1 | tr -dc '0-9')
[ "$free_gb" -ge 40 ] || die "на диске ${free_gb} ГБ, а нужно от 40 (одни признаки весят 17)"

mkdir -p "$WORK"
cd "$WORK"

# --- окружение --------------------------------------------------------------

say "Ставлю окружение"
[ -d venv ] || python3 -m venv venv
# shellcheck disable=SC1091
source venv/bin/activate
python -m pip install -q --upgrade pip

[ -d piper-sample-generator ] || git clone -q https://github.com/rhasspy/piper-sample-generator
mkdir -p piper-sample-generator/models
[ -f piper-sample-generator/models/en_US-libritts_r-medium.pt ] || wget -c -q --show-progress \
    -O piper-sample-generator/models/en_US-libritts_r-medium.pt \
    'https://github.com/rhasspy/piper-sample-generator/releases/download/v2.0.0/en_US-libritts_r-medium.pt'

[ -d openwakeword ] || git clone -q https://github.com/dscripka/openWakeWord.git openwakeword
python -m pip install -q -e ./openwakeword
python -m pip install -q piper-phonemize webrtcvad mutagen==1.47.0 torchinfo torchmetrics \
    speechbrain==0.5.14 audiomentations==0.33.0 torch-audiomentations==0.11.0 acoustics==0.2.6 \
    datasets scipy soundfile pyyaml tqdm

# --- данные -----------------------------------------------------------------

say "Скачиваю отклики комнат (MIT RIR)"
python - <<'PY'
import os
import numpy, scipy.io.wavfile
from datasets import load_dataset

out = "mit_rirs"
os.makedirs(out, exist_ok=True)
if len(os.listdir(out)) > 100:
    raise SystemExit("уже на месте")
for row in load_dataset(
    "davidscripka/MIT_environmental_impulse_responses", split="train", streaming=True
):
    name = row["audio"]["path"].split("/")[-1]
    scipy.io.wavfile.write(
        os.path.join(out, name), 16000, (row["audio"]["array"] * 32767).astype(numpy.int16)
    )
PY

say "Скачиваю общий шум (кусок AudioSet)"
if [ ! -d audioset_16k ] || [ -z "$(ls -A audioset_16k 2>/dev/null)" ]; then
    mkdir -p audioset audioset_16k
    wget -c -q --show-progress -O audioset/bal_train09.tar \
        'https://huggingface.co/datasets/agkphysics/AudioSet/resolve/main/data/bal_train09.tar'
    tar -xf audioset/bal_train09.tar -C audioset
    python - <<'PY'
import glob, os
import numpy, scipy.io.wavfile
from datasets import Audio, Dataset

files = glob.glob("audioset/**/*.flac", recursive=True)
data = Dataset.from_dict({"audio": files}).cast_column("audio", Audio(sampling_rate=16000))
for row, path in zip(data, files):
    name = os.path.splitext(os.path.basename(path))[0] + ".wav"
    scipy.io.wavfile.write(
        os.path.join("audioset_16k", name), 16000, (row["audio"]["array"] * 32767).astype(numpy.int16)
    )
PY
fi

say "Скачиваю признаки фоновой речи (17 ГБ — это надолго)"
wget -c -q --show-progress \
    "https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main/$FEATURES"
wget -c -q --show-progress \
    'https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main/validation_set_features.npy'

# --- своя музыка ------------------------------------------------------------

if [ ! -d music_16k ] || [ -z "$(ls -A music_16k 2>/dev/null)" ]; then
    cat <<'WARN'

Каталога music_16k нет или он пуст. Это **главная** часть набора: без своей
музыки получится модель, которая слышит имя в тишине.

Положи свои треки (mp3, flac, wav — как есть) в папку `music/` рядом со
скриптом и прогони:

    python3 ../prepare_audio.py music music_16k

Полчаса музыки хватает, час лучше. Своё — то, что реально играет.
WARN
    read -r -p "Продолжить без своей музыки? [y/N] " answer
    [ "$answer" = "y" ] || exit 1
    mkdir -p music_16k
fi

# --- настройка --------------------------------------------------------------

cp -f ../jarvis.yml ./jarvis.yml
if [ "$MODE" = "trial" ]; then
    say "Пробный прогон: мало примеров, мало шагов — проверяем цепочку целиком"
    python - <<'PY'
import yaml

with open("jarvis.yml") as source:
    config = yaml.safe_load(source)
config.update(
    model_name="jarvis_trial", n_samples=500, n_samples_val=200, steps=2000,
    target_false_positives_per_hour=5.0,
)
with open("jarvis.yml", "w") as target:
    yaml.dump(config, target, allow_unicode=True)
PY
fi

# --- обучение ---------------------------------------------------------------

say "Шаг 1 из 3: синтезирую произношения имени"
python openwakeword/openwakeword/train.py --training_config jarvis.yml --generate_clips

if [ -d ../my_voice ] && [ -n "$(ls -A ../my_voice 2>/dev/null)" ]; then
    say "Добавляю свои записи к синтезированным"
    # Свой голос и свой микрофон — самые ценные примеры из всех: модель нужна
    # ровно одному человеку в ровно одной комнате. Синтез даёт разнообразие,
    # эти записи — точность.
    cp -n ../my_voice/*.wav jarvis_model/positive_train/ 2>/dev/null || true
    printf 'своих записей в обучении: %s\n' "$(ls -1 ../my_voice/*.wav 2>/dev/null | wc -l)"
fi

say "Шаг 2 из 3: смешиваю с комнатой и фоном"
python openwakeword/openwakeword/train.py --training_config jarvis.yml --augment_clips

say "Шаг 3 из 3: обучаю классификатор"
python openwakeword/openwakeword/train.py --training_config jarvis.yml --train_model

say "Готово"
find . -name '*.onnx' -newermt '-1 day' -print
cat <<'DONE'

Забирай .onnx из каталога с моделью, клади в models/wakeword/jarvis.onnx и
включай в config.yaml:

  audio:
    wake_word:
      mode: acoustic
      model: models/wakeword/jarvis.onnx
      threshold: 0.5

Перед тем как радоваться — **проверь на своих записях**:

  python -m jarvis --check-wakeword models/wakeword/jarvis.onnx tools/wakeword/my_voice

Отрицательный пример ничего не доказывает: сломанная модель молчит ровно так
же, как исправная в тишине. Нужен положительный.
DONE

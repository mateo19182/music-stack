import os
import subprocess
from pathlib import Path

import pytest
from app.audio_models import AudioModels
from app.ingestion import _decode

MODELS = os.environ.get('ESSENTIA_MODELS')


def test_missing_models_are_unavailable(tmp_path):
    assert not AudioModels(tmp_path).available


@pytest.mark.skipif(not MODELS, reason='set ESSENTIA_MODELS to a directory from scripts/fetch-models.sh')
def test_real_models_describe_audio(tmp_path):
    path = tmp_path / 'beat.wav'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    "aevalsrc='if(lt(mod(t,0.5),0.05),sin(2*PI*60*t),0)+0.2*sin(2*PI*440*t)':d=20",
                    str(path)], check=True)
    models = AudioModels(Path(MODELS))
    assert models.available
    described = models.describe(_decode(path, 16000))
    assert set(described) == {'genre', 'mood'}
    assert all(isinstance(value, str) and '---' not in value for value in described['genre'])

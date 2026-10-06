"""Genre and mood estimates from Essentia's Discogs-EffNet models, used when catalogs have nothing."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import numpy as np

os.environ.setdefault('TF_CPP_MIN_LOG_LEVEL', '3')

EMBEDDINGS = 'discogs-effnet-bs64-1'
GENRE = 'genre_discogs400-discogs-effnet-1'
MOODS = {'mood_happy': 'Happy', 'mood_sad': 'Sad', 'mood_aggressive': 'Aggressive',
         'mood_relaxed': 'Relaxed', 'mood_party': 'Party', 'danceability': 'Danceable'}
MOOD_THRESHOLD = 0.65
# Most hip hop and funk scores as danceable at 0.65; only tag the clear cases.
THRESHOLDS = {'danceability': 0.85}
SAMPLE_RATE = 16000


def _head(path):
    """Input, output and positive-class index from a classifier's metadata file."""
    metadata = json.loads(path.with_suffix('.json').read_text())
    schema = metadata['schema']
    output = next((o for o in schema['outputs'] if o.get('output_purpose') == 'predictions'), schema['outputs'][0])
    return schema['inputs'][0]['name'], output['name'], metadata['classes']


class AudioModels:
    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.Lock()
        self.models = None

    def files(self):
        names = [EMBEDDINGS, GENRE, *(f'{name}-discogs-effnet-1' for name in MOODS)]
        return [self.root / (name + '.pb') for name in names] + [self.root / (name + '.json') for name in names[1:]]

    @property
    def available(self):
        try:
            import essentia.standard as es
        except ImportError:
            return False
        return hasattr(es, 'TensorflowPredictEffnetDiscogs') and all(path.is_file() for path in self.files())

    def _load(self):
        import essentia
        import essentia.standard as es
        essentia.log.infoActive = False
        essentia.log.warningActive = False
        heads = {}
        for name in [GENRE, *(f'{mood}-discogs-effnet-1' for mood in MOODS)]:
            graph = self.root / (name + '.pb')
            input_name, output_name, classes = _head(graph)
            heads[name] = (es.TensorflowPredict2D(graphFilename=str(graph), input=input_name, output=output_name), classes)
        return es.TensorflowPredictEffnetDiscogs(graphFilename=str(self.root / (EMBEDDINGS + '.pb')),
                                                  output='PartitionedCall:1'), heads

    def describe(self, audio):
        """Genres and moods for mono float32 audio at 16 kHz."""
        with self.lock:
            if self.models is None:
                self.models = self._load()
            embed, heads = self.models
            embeddings = embed(np.ascontiguousarray(audio, dtype=np.float32))
            genre_model, genre_classes = heads[GENRE]
            scores = np.mean(genre_model(embeddings), axis=0)
            moods = []
            for name, label in MOODS.items():
                model, classes = heads[f'{name}-discogs-effnet-1']
                positive = next(i for i, c in enumerate(classes) if not c.startswith(('non_', 'not_')))
                if float(np.mean(model(embeddings), axis=0)[positive]) >= THRESHOLDS.get(name, MOOD_THRESHOLD):
                    moods.append(label)
        ranked = np.argsort(scores)[::-1]
        top = float(scores[ranked[0]])
        # Labels read "Hip Hop---Boom Bap"; the style after the dashes is the useful part.
        styles = [genre_classes[i].split('---')[-1] for i in ranked[:2]
                  if float(scores[i]) >= 0.1 and float(scores[i]) >= top / 2]
        return {'genre': styles, 'mood': moods}


_loaded = {}
_loaded_lock = threading.Lock()


def models_at(root):
    """One loaded model set per directory; loading TensorFlow graphs is slow."""
    with _loaded_lock:
        return _loaded.setdefault(str(root), AudioModels(root))

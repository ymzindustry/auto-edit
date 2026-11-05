# 📜 Scripts de montage vidéo

Tout est dans le profil : `scripts/` (le code), les environnements Python
(`venv_audio/`, `venv_video/`, `venv_stt/`), `assets/` (fonds + musiques),
`raw_data/` (sources et résultats).

```
scripts/
    workflows/          les 5 ÉTAPES du pipeline (fragmentation → final)
    effects/            les EFFETS : un fichier .py par effet
    test_workflow.py    la chaîne complète, en un seul fichier
    test_effects.py     les PARAMÈTRES des effets (entrée de effects_workflow)
    test_pipeline.sh    les mêmes commandes, prêtes à coller dans le shell
    *.py                les fonctions appelées par les étapes (audio, découpe, fond…)
```

## ⚡ Le pipeline en 5 étapes (un seul dossier de travail)

**Chaque étape est indépendante** et accepte plusieurs formes d'entrée — plus besoin de
les enchaîner dans l'ordre, ni que le dossier vienne de `fragmentation/` :

| ce que tu passes | ce qui est lu |
|---|---|
| `shoulder` (dossier projet) | `shoulder/fragmentation` — trouvé tout seul |
| `shoulder/fragmentation` (le dossier d'étape) | lui-même |
| `n/importe/quel/dossier` | s'il contient **directement** des `segment_i/` (des `.mp4` + `.txt`) |
| `.../fragmentation/segment_3` | son dossier parent (pratique pour un test) |

et `--out <dossier>` envoie le résultat où tu veux.

```bash
python3 scripts/workflows/fragmentation_workflow.py raw_data/ma_video   # sources -> fragmentation/ (découpe + transcription)
python3 scripts/workflows/greenscreen_workflow.py raw_data/ma_video     # -> greenscreen/ (fond vert)
python3 scripts/workflows/background_workflow.py raw_data/ma_video --background assets/bg_anime_room.mp4   # -> background/
python3 scripts/workflows/effects_workflow.py --plan scripts/test_effects.py --input raw_data/ma_video         # -> effects/
python3 scripts/workflows/final_workflow.py raw_data/ma_video assets/music_relax.mp3   # -> final/final.mp4
```

> Les étapes sont dans `scripts/workflows/`, les effets dans `scripts/effects/` : les
> deux dossiers ne se mélangent pas. Les anciens `workflow1..5.py` (alias) et les scripts
> morts (montage, to_greenscreen, add_music, pipeline…) ont été **supprimés** ; une
> archive est dans `backups/` si besoin.

### 1. `fragmentation_workflow.py <dossier>` — audio + découpe + transcription → `fragmentation/`

Le dossier contient :

- **un `.wav`** = l'audio de référence de la prise (il sera amélioré), **et**
- **un ou plusieurs `.mp4`** dont l'audio vient de la **même prise** que le `.wav`.

C'est cette source commune qui permet de **retrouver où commence chaque vidéo dans
le `.wav`** (corrélation croisée) : toutes les vidéos sont donc placées sur **une
seule ligne de temps**.

> **Pas de `.wav` dans le dossier ?** L'audio de **`main.mp4`** sert de référence :
> il est extrait puis amélioré exactement comme le `.wav` (même routine).
> Autre nom de fichier : `--main monfichier.mp4`.

Étapes internes : **les silences sont détectés sur l'audio BRUT et supprimés
AVANT l'amélioration** — seul le reste (la parole) est nettoyé. Sur un
enregistrement normal, 40 à 70 % du fichier est du silence : on économise tout ce
traitement, et la mesure de volume se fait sur la parole seule. Une carte JSON
(`speech_map.json`) retient où vit chaque intervalle dans ce fichier plus court,
pour que la synchro reste exacte.

- L'audio de **chaque segment** est la parole améliorée à cet instant précis :
  synchro parfaite, quelles que soient les coupes.
- L'image vient de la ou des vidéos qui couvrent cet instant : **un segment peut
  donc contenir plusieurs vidéos** (recollées bout à bout).
- Un trou sans vidéo devient **du noir** (`--gap black`, défaut) ou est **supprimé**
  (`--gap skip`).
- `--noise` vaut **`auto`** par défaut : le seuil est déduit du bruit de fond de
  l'enregistrement (+6 dB) — indispensable sur un fichier qui souffle, où un seuil
  fixe à -35 dB ne trouve aucun silence. Donne un nombre pour le forcer.
- `--no-pre-cut` rétablit l'ancien ordre (tout améliorer, puis couper).

**Format : chaque caméra garde le sien.** Rien n'est forcé sur un canvas commun : une
capture **portrait** (1080x2400) reste portrait (864x1920 avec `--canvas 1920x1080`),
elle n'est jamais mise en paysage avec des bandes noires. `--canvas WxH` n'est qu'un
**plafond**, appliqué dans le bon sens selon l'orientation ; sans lui, chaque vidéo
garde sa taille d'origine exacte.

**Nommage des segments :** **un dossier par segment, un fichier par caméra dedans** :

```
<dossier>/fragmentation/
    segment_0/                <- les segments sont numérotés À PARTIR DE 0
        main_0.mp4            <- le MÊME instant de la timeline,
        secreenvideo_0.mp4    <- même durée, même audio, synchronisés
        segment_0.txt         <- la transcription de cet instant
    segment_1/ ...
    order.txt         <- l'ordre de lecture (indispensable aux étapes suivantes)
    sync.json         <- décalages, confiances, trous, plan par caméra
    transcription.txt <- TOUT le texte, segment par segment (vue globale)
```

**`transcription.txt` — la vue globale du texte.** Écrit à la fin de l'étape 1 : un bloc
par segment, dans l'ordre de lecture, avec sa position **sur la prise** (`prise
00:06.300→00:13.837`), sa position **dans le montage final** (`montage
00:00.000→00:07.537`), sa durée, son nombre de mots, les caméras du segment, puis le
texte. En tête : la source audio, les caméras, le nombre de segments gardés/supprimés et
le total de parole. Régénérable seul, sans relancer la découpe :

```bash
python3 scripts/transcription_overview.py shoulder/fragmentation      # -> transcription.txt
python3 scripts/transcription_overview.py shoulder                    # la racine du projet marche aussi
python3 scripts/transcription_overview.py shoulder/fragmentation --out /tmp/texte.txt
```

Une caméra qui ne tournait pas à cet instant reçoit des **images noires** (jamais un
fichier plus court : les durées doivent rester égales, c'est tout l'intérêt du duo).

**La numérotation se refait sur les segments gardés** : après la suppression des
segments vides (« rien de dit » / hallucination), les survivants sont renumérotés
1, 2, 3… **sans trou** ; l'ancien numéro reste dans `sync.json` (`"original_name"`).
`--keep-numbering` garde au contraire les numéros tels qu'ils ont été coupés.
Un nouveau passage sur le même dossier **efface les dossiers du passage précédent**.

```
<dossier>/fragmentation/
    camA_1/   ->  camA_1.mp4  +  camA_1.txt
    camA_2/   ->  ...
    camB_1/   ->  ...
    order.txt    <- l'ordre de lecture (indispensable aux étapes suivantes)
    sync.json    <- décalages, confiances, trous, plan de découpe
```

Options utiles :

```bash
--noise auto|-35 --min-silence 0.3 --margin 0.3   # réglages de la coupe (avant l'amélioration)
--no-pre-cut                                   # ancien ordre : tout améliorer puis couper
--gap black|skip                               # trous sans vidéo
--prefer cover|tight                           # 2 vidéos couvrent le même instant
--video-offset camB.mp4=12.63                  # forcer un décalage (si détection faible)
--numbering per-video|global                   # nommage des segments
--keep-numbering                               # ne pas renuméroter après les suppressions
--skip-enhance                                 # pas de débruitage (test / re-run rapide)
--no-transcribe                                # garder tous les segments
```

**Un aperçu est écrit à la fin : `fragmentation/preview_720p.mp4`.** Tous les segments gardés sont
recollés dans l'ordre, avec l'audio amélioré, en **1280x720 / 30 fps / crf 28** — quelques
centaines de Ko au lieu de plusieurs Go. C'est le fichier à regarder pour **juger la
découpe** : une phrase tronquée, une respiration gardée, un blanc qui traîne sautent aux
yeux.

L'aperçu est fabriqué par **`scripts/preview.py`**, le même outil utilisable seul sur
n'importe quel dossier d'étape :

```bash
python3 scripts/preview.py raw_data/shoulder/fragmentation   # -> fragmentation/preview_720p.mp4
python3 scripts/preview.py raw_data/shoulder                 # prend la DERNIÈRE étape trouvée
python3 scripts/preview.py raw_data/shoulder/fragmentation --cam screen --height 480 -o /tmp/pv.mp4
```

Il choisit **une caméra par segment** (`main` par défaut), réencode chaque segment à la
taille de l'aperçu, puis les colle avec `-c copy` : deux passes courtes plutôt qu'un
filtre géant qui casse sur les longues prises ou quand les caméras n'ont pas la même
taille. La cadence est plafonnée à 30 fps **sans jamais rééchantillonner une caméra plus
lente** (c'est ce qui donnait l'impression de saccade).

**Il affiche sa progression** : un segment = une ligne, avec le pourcentage en direct
quand la sortie est un terminal (`[12/64] segment_11/main_11.mp4  45%  6.2s`), et une
ligne par segment quand la sortie part dans un fichier de log.

**Le temps de calcul, mesuré sur cette machine (8 cœurs)** : environ **2,3x la durée de
la prise** — 64 segments / 11 min de 4K = ~5 min. Le mur, c'est le **décodage du 4K**
(un seul ffmpeg prend déjà ~750 % de CPU) :

| réglage | effet mesuré |
|---|---|
| `--preset ultrafast` | aucun gain (le décodage domine) |
| `--height 480` | aucun gain, fichier plus léger |
| `--jobs 4` | aucun gain (~750 % de CPU par ffmpeg) |
| `--abitrate 96k` (défaut) | aucun gain de temps, fichier plus léger |
| `--audio-copy` | piste voix **bit-exacte**, fichier plus gros |

Autrement dit : ni l'audio ni la parallélisation ne raccourcissent le calcul, c'est du
décodage 4K pur.

| option | effet |
|---|---|
| `--preview-height 1080` | aperçu plus net (1080 au lieu de 720) |
| `--preview-fps 25` | encore plus léger |
| `--preview-crf 23` | meilleure image, fichier plus gros |
| `--preview-cam screen` | montrer une autre caméra que `main` |
| `--no-preview` | pas d'aperçu du tout |
| `--preview-only` | reconstruire l'aperçu d'un `fragmentation/` existant, sans rien recouper |

Si la découpe ne te plaît pas, change `--noise`, `--min-silence` ou `--margin` et
relance : l'aperçu est refait à chaque passage.

**Rien n'est converti par défaut.** `fragmentation_workflow` écrit chaque caméra à **sa taille
d'origine** (`--canvas native`, défaut), en gardant son orientation : une capture
portrait 1080x2400 reste 1080x2400, ton 4K reste 4K. Pour obtenir du 1080p, demande-le :

```bash
python3 scripts/workflows/fragmentation_workflow.py shoulder --to-1080p   # = --canvas 1920x1080
```

Le plafond est appliqué **dans le sens de chaque caméra** (portrait 1080x2400 →
864x1920), jamais en paysage avec des bandes noires.

### 2. `greenscreen_workflow.py <entrée>` — chaque segment sur fond vert → `greenscreen/`

`--seg-model u2netp` (IA, par défaut) ou `--seg-model without` (chroma key rapide).
C'est l'étape chère : on la fait **une fois**. Le nombre de fichiers est identique à `fragmentation/`.

**`greenscreen_workflow` ne convertit rien et ne bloque jamais.** Aucune erreur si une vidéo est en
4K : un simple avertissement, puis il continue avec les fichiers tels quels.

```
  ! WARNING: 2 file(s) are bigger than a 1080p frame, e.g. segment_1/main_1.mp4 = 3840x2160
  !          nothing is converted and the run continues, but such a frame needs more RAM
  !          than this machine has (3.8 GB): the kernel may kill it.
```

Pour convertir ici quand même : `--max-size 1920x1080` (conversion explicite, la sortie
est alors plus petite que la source). Et un run tué par le noyau **reprend tout seul** :
les fichiers déjà produits sont gardés, `--redo` pour tout recalculer.

Un segment contient **un fichier par caméra**. `--only` choisit celles à traiter (par
préfixe de nom de fichier) ; les autres sont **recopiées telles quelles** :

```bash
python3 scripts/workflows/greenscreen_workflow.py raw_data/ma_video                        # toutes les caméras → vert
python3 scripts/workflows/greenscreen_workflow.py raw_data/ma_video --only main            # seulement main_1, main_2...
python3 scripts/workflows/greenscreen_workflow.py raw_data/ma_video --only main --only screen2
```

Résultat : `greenscreen/segment_1/{main_1.mp4 (vert), screen_1.mp4 (inchangé)}`.

### 3. `background_workflow.py <entrée> --background <image|vidéo>` — remplace le vert → `background/`

Chroma key rapide, **aucune re-segmentation** : on peut essayer autant de fonds qu'on
veut pour presque rien. Le fond vidéo avance en continu (pas de saut aux jonctions).
Même filtre `--only` qu'à l'étape 2 (les fichiers sans vert sont recopiés tels quels).

### 4. `effects_workflow.py --plan <fichier.py>` — applique un **plan d'effets** → `effects/`

Le workflow est un **moteur générique** : il ne connaît aucun effet, il applique un
**plan**. Un **effet = un fichier `.py`** dans `scripts/effects/`, et le **plan = un
fichier `.py`** à part (`scripts/test_effects.py` par défaut).

```bash
python3 scripts/workflows/effects_workflow.py --plan scripts/test_effects.py --input raw_data/ma_video
```

- `--plan` : le fichier des **paramètres** des effets (c'est LUI qu'on édite) ;
- `--input` / `--output` : le dossier lu / écrit pour ce run (sinon le bloc **CONFIG**
  en haut de `effects_workflow.py`) ;
- `--dry-run` : affiche le plan résolu, ne rend rien.

Le fichier de plan est du Python normal : il importe les effets et définit `PLAN` :

```python
from effects import side_by_side

PLAN = [
    {"segments": "all",
     "effect": side_by_side(base="main", element="screen",
                            position="left", height_pct=100, gap_pct=1.0)},
]
```

- `"segments"` = position dans l'ordre de lecture, nom du dossier, `range(...)`, ou
  **`"all"`** (tous les segments) ; certains effets acceptent aussi **`all=True`** dans
  l'appel (voir `zoom`), et l'entrée peut alors ne pas avoir de `"segments"` du tout ;
- **en sortie, les dossiers sont toujours renumérotés `segment_0`, `segment_1`, …** : une
  plage fusionnée donne UN dossier `segment_N/segment_N.mp4` (le nom des segments
  fusionnés n'apparaît jamais dans l'arborescence) ;
- **`fusion=True`** (paramètre de l'effet) : les segments visés sont **fusionnés en un
  seul clip** (concaténation sans réencodage) et l'effet s'applique **une fois** dessus
  → aucun redémarrage ni coupure aux jonctions. Le résultat du groupe devient **un seul
  dossier** (`segment_1_3/`) et `order.txt` est réécrit en conséquence ;
- sans `fusion`, l'effet s'applique segment par segment (la continuité est assurée par
  le calcul de fenêtre), et chaque segment est réencodé séparément ;
- un effet qui **s'étale** (comme `add_media`) **couvre aussi les segments entre le
  premier et le dernier** : `[1, 3]` couvre 1, 2 et 3 — sinon le média disparaîtrait
  pendant le 2 et reviendrait sans fondu ;
- les segments non touchés sont recopiés tels quels.

**Ajouter un effet** = déposer `effects/mon_effet.py` (une fonction `mon_effet(...)`
qui décrit l'effet + une fonction `command(...)` qui construit la commande) : il est
aussitôt utilisable dans le plan, sans toucher au moteur.

**Les quatre effets livrés :**

- **`side_by_side`** — les deux caméras **côte à côte**, toutes les deux entièrement
  visibles (aucun recadrage) : `base="main"`, `element="screen"`, `position="left"`
  (l'écran à gauche de la main) ou `"right"`, `height_pct` (hauteur de l'élément en %
  de l'image), `gap_pct` (bande noire entre les deux, en % de la largeur), `pad`
  (couleur du fond). Le cadre s'élargit : une main 4K reste 2160 px de haut, l'écran
  portrait reste portrait.
- **`zoom`** — **travelling avant** sur la caméra : `zoom` (1.0 = rien, 2.0 = l'image fait
  2 fois sa taille à la fin), `start` (secondes avant que le mouvement commence),
  `end` (le mouvement est fini `end` secondes avant la fin du segment), `crf`, `preset`,
  `fusion`, `only`, **`each`** et **`min`** : `each=False` (défaut) = **un seul mouvement
  continu** sur toute la plage (avec `fusion=True`) ; `each=True` = le zoom **repart de
  1.0 sur chaque segment** (chacun fait son propre 1 → zoom entre son `start` et son
  `dur - end`, et `fusion` est alors ignoré). Le mouvement couvre donc **toujours**
  `start` → `dur - end`. **`min`** est uniquement un **filtre de sélection** : seuls les
  segments **plus longs** que `min` reçoivent l'effet (2 s avec `min=2` → rien), les
  autres sont recopiés tels quels — en mode `fusion=True` le seuil s'applique à la plage
  entière.
- **`add_media`** — pose une image/vidéo par-dessus la caméra (`src`, `pct`, `blur`,
  `fade_in`, `fade_out`, `start`, `end`, `only`, `fusion`, `fit`). **`fit`** décide de ce
  qui se passe quand le média est **plus long** que la fenêtre où il doit tenir :
  `"cut"` (défaut) le joue et le coupe à la fin de la fenêtre ; `"speed"` l'**accélère**
  (image + son, `setpts` + `atempo`) pour que **tout le média** tienne exactement dans la
  fenêtre — un clip de démo de 60 s qui doit tenir en 18 s. Un média plus court continue
  de boucler. `volume` (0..1, défaut 0.3)
  règle le **son du média incrusté**, mixé sous la voix de la vidéo pour éviter les
  conflits. `only="main"` limite l'effet à une caméra (les autres sont recopiées).
- **`push_video`** — **v2 entre et pousse v1** : `base="main"` (v1), `element="screen"`
  (v2, entièrement visible), `direction="right|left|up|down"`, `pct` (taille de v2 en
  travers de la frame, % de la frame), **`percent` = part de V2 posée sur v1 (en % de
  v2)** — c'est elle qui règle la poussée de v1 (`poussée = taille de v2 − recouvrement` :
  `0` = côte à côte, `25` = un quart de v2 sur v1, `100` = v1 pas poussé du tout) —
  `front=True` (v2 devant v1 dans la bande de recouvrement ; `False` = derrière),
  `fade_in` = durée du coulissement. Les deux caméras du segment sont composées en
  **une seule image** (l'effet consomme les deux fichiers).

### 5. `final_workflow.py <entrée> <musique>` — assemblage + musique → `final/final.mp4`

Recolle tous les segments dans l'ordre (`order.txt`) et pose une musique en fond
(volume `--volume`, fondus `--fade`).

## 🧪 La chaîne de test, en un seul fichier

`scripts/test_workflow.py` enchaîne **tout** sur un extrait de 10 s de
`raw_data/video` (les fichiers complets font 3 min : inutile d'attendre) :

```
0. CUT       main.MP4 + screen.mp4 -> les 10 premières secondes
1. FRAGMENT  coupe des silences + amélioration de la voix (DeepFilterNet)
2. GREEN     fond VERT par u2net, caméra `main` UNIQUEMENT   (--only main)
3. CARBG     ce vert est remplacé par carbg.png, `main` UNIQUEMENT
4. SCREEN    l'écran à GAUCHE de la main (paramètres : scripts/test_effects.py)
```

```bash
python3 scripts/test_workflow.py                     # tout, 10 s
python3 scripts/test_workflow.py --seconds 6         # extrait plus court
python3 scripts/test_workflow.py --from 3            # reprend à l'étape 3
python3 scripts/test_workflow.py --stage 4           # seulement le côte à côte
python3 scripts/test_workflow.py --dry-run           # affiche les commandes
```

Résultat : `raw_data/video/test10s/{fragmentation,greenscreen,background,effects}/`
et `raw_data/video/test10s_final.mp4`.
À la fin il **vérifie chaque étape** (durée, taille, décodage sans erreur) et affiche
`ALL GOOD`.

Deux options utiles : `--offset screen.mp4=1.445` force la position d'une caméra quand
la corrélation audio est faible sur un extrait court, et `--transcribe` ajoute la
transcription (désactivée par défaut : sur 10 s de silence Whisper hallucine et les
segments sont jetés).

Les **mêmes commandes**, prêtes à coller dans le shell : `scripts/test_pipeline.sh`
(`./scripts/test_pipeline.sh 3` reprend à l'étape 3).

## 🔤 Audio (env : `venv_audio/`)

| Script | Rôle |
|---|---|
| `speech_master.py` | **Supprime les silences AVANT l'amélioration** : découpe le brut, améliore la parole seule, écrit la carte JSON |
| `enhance_audio.py` | Débruitage (DeepFilterNet) + EQ + compression + loudnorm |
| `enhance_variants.py` | Plusieurs variantes d'amélioration d'un extrait, à la même loudness, pour choisir à l'oreille |
| `sync_offsets.py` | **Où commence chaque `.mp4` dans le `.wav`** (JSON) |
| `cut_timeline.py` | Coupe la ligne de temps et sort l'image des vidéos (`--audio-map`) |
| `sync_replace_audio.py` | Outils partagés (audio mono, corrélation) utilisés par les étapes |
| `cut_silences.py` | Détection de silence / seuil `auto` partagé par les étapes |

## 🧪 Tests / vérification

| Script | Rôle |
|---|---|
| `preview.py` | **Aperçu léger** d'un dossier d'étape : segments recollés en 720p/30 fps |
| `verify_fragmentation.py` | **Vérifie un `fragmentation/`** : durées, synchro audio vs la référence, image source |
| `test_workflow.py` / `test_effects.py` / `test_pipeline.sh` | la chaîne de test, en un fichier |

```bash
python3 scripts/test_workflow.py                    # chaîne complète sur 10 s
python3 scripts/verify_fragmentation.py shoulder    # doit finir par ALL GOOD
```

## 🎥 Vidéo / fond (env : `venv_video/`)

| Script | Rôle |
|---|---|
| `apply_background.py` | Fond (image/vidéo) ou fond vert + segmentation (appelé par greenscreen/ et background/) |
| `insert_media.py` | Incruste une image/vidéo (fondu + arrière-plan flouté), appelé par l'effet `add_media` |

## 📖 Où sont les explications

Chaque étape commence par un **en-tête unique** : ce que fait le fichier, **tous les
paramètres**, puis deux ou trois exemples de commande. Le corps du code n'a **aucun
commentaire** — tout est dans l'en-tête (et dans `--help`). Exemple :

```bash
python3 scripts/workflows/fragmentation_workflow.py --help
```

```python
# --noise      : SEUIL DE SILENCE en dB. Tout ce qui est plus faible est « silence »
#                et sera coupé. -35 par défaut ; baisse-le (-45) si l'enregistrement
#                souffle et que les silences ne sont pas détectés, monte-le (-25)
#                si les respirations sont conservées.
ap.add_argument("--noise", type=float, default=-35.0, help="...")
```

Les valeurs indiquées après `default=` sont celles utilisées quand tu ne mets
rien : `(default 0.3)` = 0,3 s si tu ne passes pas l'option.

## ⏱️ Temps d'exécution

- Segmentation IA (`u2net`/`u2netp`) : **lente** (~1 image/s sur CPU).
  L'étape 3 (chroma key) est quasi instantanée.
- Amélioration audio : DeepFilterNet en tranches de 30 s (~1 Go de RAM au pic).
- Coupes / transcription : quelques minutes.

## 📝 Notes

- Le code (scripts, commentaires) est en **anglais** ; cette doc est en français.
- `setup.sh` réinstalle les environnements depuis zéro si besoin.
- Tout reste dans `raw_data/` (lisible via Telegram) ou dans le profil.


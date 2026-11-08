# Composants tiers

Upscale est distribué sous **AGPL-3.0** (voir [LICENSE](LICENSE)).

Les images Docker publiées embarquent ou exécutent des composants tiers, chacun sous sa propre licence.
Les obligations de ces licences (notamment AGPL-3.0) s'appliquent à leur redistribution : les sources
correspondantes sont disponibles aux adresses ci-dessous.

| Composant | Rôle | Licence | Source |
|---|---|---|---|
| Video2X | Moteur d'upscaling et d'interpolation (`Dockerfile.local`) | AGPL-3.0 | https://github.com/k4yt3x/video2x |
| upscayl-ncnn (`upscayl-bin`) | Moteur des modèles personnalisés (Vulkan) | AGPL-3.0 | https://github.com/upscayl/upscayl-ncnn |
| Real-ESRGAN | Modèles et algorithmes | BSD-3-Clause | https://github.com/xinntao/Real-ESRGAN |
| ncnn | Inférence Vulkan | BSD-3-Clause | https://github.com/Tencent/ncnn |
| spandrel | Lecture des modèles PyTorch | MIT | https://github.com/chaiNNer-org/spandrel |
| pnnx | Conversion PyTorch → ncnn | non précisée dans le dépôt (à vérifier avant redistribution) | https://github.com/pnnx/pnnx |
| PyTorch | Conversion des modèles | BSD-3-Clause | https://github.com/pytorch/pytorch |
| FFmpeg | Découpe, encodage, assemblage | LGPL-2.1+ / GPL selon la compilation | https://ffmpeg.org/ |
| Flask, Waitress | Service HTTP | BSD-3-Clause, ZPL-2.1 | https://flask.palletsprojects.com/ |
| Docker CLI | Pilotage du conteneur Video2X | Apache-2.0 | https://github.com/docker/cli |
| Police Manrope | Interface | SIL OFL 1.1 | https://github.com/sharanda/manrope |

Les modèles installés depuis OpenModelDB ou importés par l'utilisateur ne sont pas distribués avec
Upscale : chaque fiche de modèle indique sa licence (certaines interdisent l'usage commercial).

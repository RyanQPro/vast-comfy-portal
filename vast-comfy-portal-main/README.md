# vast-comfy-portal : modèles ComfyUI à la demande sur Vast.ai

Le template ComfyUI officiel de Vast.ai (`vastai/comfy`) télécharge tous les modèles
d'un `PROVISIONING_SCRIPT` au démarrage, et ComfyUI attend que ce soit fini. Ce repo
fournit une version modifiée du dashboard de l'instance (l'*Instance Portal*, port
1111) avec une page **Models** : l'instance démarre **sans modèle**, et on télécharge
seulement ce dont on a besoin.

Basé sur le portail de [vast-ai/base-image](https://github.com/vast-ai/base-image)
(v3.1.8), modifié : voir [NOTICE.md](NOTICE.md).

## Utiliser le template

Pars du template ComfyUI officiel de Vast, puis dans ses variables d'environnement :

| Variable | Valeur | Pourquoi |
|---|---|---|
| `PORTAL_REPO` | `<compte>/<repo>` (le repo GitHub qui porte la release) | Installe ce portail au premier démarrage |
| `PORTAL_VERSION` | `v3.1.8-models.2` | La release à installer |
| `PROVISIONING_SCRIPT` | *(supprimer, ou un script qui n'installe que tes custom nodes)* | Plus aucun modèle téléchargé au boot |
| `MODEL_CATALOG` | *(optionnel)* URL d'un catalogue YAML | Tes setups et ta liste sur chaque nouvelle instance |

Garde le reste tel quel (`PORTAL_CONFIG`, `COMFYUI_ARGS`, ports…).

Mets **`HF_TOKEN`** (et `CIVITAI_TOKEN` si besoin) dans *Account → Settings →
Environment variables* de Vast, **pas** dans le template, surtout s'il est public.
Sans `HF_TOKEN`, les modèles protégés (ex. FLUX.1 dev de black-forest-labs)
affichent *Login needed*. Il faut aussi accepter la licence sur la page du modèle.

> **Pourquoi `PORTAL_REPO` et pas `PORTAL_DOWNLOAD_URL` ?** L'image `vastai/comfy`
> actuelle est construite sur une base de septembre 2026, dont le script
> `update-portal` gérait mal `PORTAL_DOWNLOAD_URL` (corrigé upstream le 2026-10-06 :
> archive écrite dans `/`, fichier `VERSION` vidé). Le chemin release GitHub
> (`PORTAL_REPO` + `PORTAL_VERSION`) fonctionne sur l'ancienne comme sur la nouvelle
> version.

## La page Models

- **Setups** (si ton catalogue en définit) : un bloc par famille de modèles (ex.
  *Krea 2*, *MOSS*). **Install setup** télécharge d'un coup le modèle de base, le VAE
  et le text encoder. Taille totale et accès (*Login needed*) sont vérifiés avant le
  clic, et un avertissement s'affiche si l'espace disque ne suffit pas. En dessous,
  les **LoRAs** et **Other models** du groupe se cochent et se téléchargent à la
  carte. Un fichier commun à plusieurs setups (ex. un VAE) n'est téléchargé qu'une
  fois. Voir [Setups et LoRAs](#setups-et-loras).
- **Add models** : colle un ou plusieurs liens (un par ligne). Avant de télécharger,
  chaque lien est vérifié :
  - les liens Hugging Face copiés depuis le navigateur (`/blob/`) sont convertis en
    liens de téléchargement (`/resolve/`) ;
  - le dossier ComfyUI est deviné (`loras`, `vae`, `text_encoders`,
    `diffusion_models`, `upscale_models`…) et reste modifiable. S'il ne peut pas être
    deviné (ex. un gros `.safetensors` qui peut être un checkpoint ou un
    diffusion_model), la case est surlignée et il faut choisir ;
  - la taille, le nom du fichier (renommable) et l'accès s'affichent : *Ready*,
    *Login needed* (modèle protégé → `HF_TOKEN`), *Not found*, *Installed* ;
  - CivitAI : page de modèle ou lien de téléchargement ; nom, type et taille viennent
    de l'API CivitAI.
- **Downloads** : progression, vitesse, temps restant, annulation, relance. Deux
  téléchargements en parallèle (réglable). Un téléchargement interrompu reprend où il
  s'était arrêté.
- **My list** : les liens gardés (case *Save to My list*). *Download missing*
  retélécharge tout ce qui manque ; *Export YAML* produit un fichier réutilisable.
- **Installed** : les fichiers présents par dossier, avec suppression.

Les nouveaux fichiers apparaissent dans ComfyUI après un rafraîchissement (touche
**R** dans ComfyUI).

## Garder sa liste d'une instance à l'autre

Les modèles et *My list* vivent dans `/workspace` : ils survivent à un stop/start,
pas à la destruction de l'instance (sauf volume). Pour retrouver ta liste :

1. *Export YAML* dans **My list** ;
2. héberge le fichier (gist GitHub → bouton *Raw*, ou ce repo) ;
3. mets son URL dans `MODEL_CATALOG`. Elle apparaît dans **My list** (en lecture
   seule, marquée *catalog*), et *Download missing* récupère tout d'un coup.

Format : voir [model-list.example.yaml](model-list.example.yaml).
`MODEL_CATALOG` accepte plusieurs URLs ou chemins séparés par des virgules.

**Catalogue privé.** Un gist « secret » reste lisible par quiconque a le lien. Mets
plutôt le catalogue dans un **repo Hugging Face privé** : le portail le télécharge
avec ton `HF_TOKEN` (comme les modèles), par exemple
`MODEL_CATALOG=https://huggingface.co/<toi>/comfy/resolve/main/catalog.yaml`.

## Setups et LoRAs

Un catalogue peut définir des `groups`, affichés en haut de la page Models :

```yaml
groups:
  - name: Krea 2
    setup:                    # installé d'un clic : « dossier ComfyUI: lien »
      diffusion_models: https://huggingface.co/<toi>/comfy/resolve/main/diffusion_models/model.safetensors
      vae: https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/Wan2_1_VAE_fp32.safetensors
      text_encoders: https://huggingface.co/Comfy-Org/Krea-2/resolve/main/text_encoders/qwen3vl_4b_bf16.safetensors
    extras:                   # même format, à la carte
      diffusion_models: [https://..., https://...]
    loras: loras/krea         # dossier du repo HF qui héberge le catalogue
```

- **Changer un setup** : modifie le lien dans `catalog.yaml`, directement sur
  huggingface.co (bouton *Edit*). Le portail relit le catalogue après une minute
  au plus : rien à redéployer.
- **`loras`** : un dossier du repo qui contient le catalogue (chemin relatif), ou un
  lien de dossier `https://huggingface.co/<repo>/tree/main/<dossier>`. Le portail
  liste son contenu via l'API Hugging Face : **dépose une LoRA dans le dossier sur
  HF, elle apparaît dans le portail**. On peut aussi donner des liens de fichiers,
  ou une liste qui mélange les deux. Les fichiers vont tous dans `models/loras/`
  (sans sous-dossier, pour que tes workflows existants les retrouvent).
- `name` est obligatoire ; `description` et `id` (nom court, sert aussi aux personnages) sont optionnels.

### Personnages

Pour des LoRAs de personnages disponibles sur plusieurs modèles, ajoute en haut du
catalogue `characters: loras/characters` et donne un `id` court à chaque groupe :

```
loras/characters/
├── Alice/
│   ├── cover.jpg          # image carrée, directement dans le dossier : la carte
│   ├── krea/…safetensors  # sous-dossier = id du groupe
│   └── moss/…safetensors
└── Bob/
    └── krea/…safetensors
```

Chaque groupe affiche une carte (image, nom, bouton *Download*) pour chaque
personnage qui a un sous-dossier à son `id`. Un nouveau personnage, c'est juste un
nouveau dossier, sans toucher au catalogue. L'image peut être dans un repo privé :
le portail la récupère avec `HF_TOKEN` (route `/models/image/…`, limitée aux images
qu'il a lui-même listées). Formats : jpg, png, webp ; 512×512 suffit.

Organisation conseillée, en un seul repo privé :

```
<toi>/comfy
├── catalog.yaml
├── diffusion_models/      # tes modèles privés utilisés par les setups
└── loras/
    ├── krea/              # LoRAs par modèle (id du groupe)
    ├── moss/
    └── characters/        # un dossier par personnage
```

## Variables d'environnement

| Variable | Défaut | Rôle |
|---|---|---|
| `MODEL_MANAGER_DIR` | `$WORKSPACE/ComfyUI/models` | Dossier des modèles |
| `MODEL_MANAGER_CONCURRENCY` | `2` | Téléchargements simultanés |
| `MODEL_LIST_FILE` | `$WORKSPACE/model-list.yaml` | Fichier de *My list* |
| `MODEL_CATALOG` | — | Catalogues : setups + liste (URLs/chemins, séparés par `,`) |
| `HF_TOKEN` / `CIVITAI_TOKEN` | — | Envoyés uniquement à leur propre site, en https |

## Sécurité

- Les routes `/models/*` passent par la même authentification Caddy que le reste du
  portail (token `OPEN_BUTTON_TOKEN` / `WEB_PASSWORD`).
- Les tokens ne partent que vers `huggingface.co` / `civitai.com`. Lors des
  redirections vers les CDN ou le stockage pré-signé, `httpx` retire l'en-tête
  `Authorization` (vérifié par un test).
- Écriture dans `fichier.part`, puis renommage atomique : ComfyUI ne voit jamais de
  fichier à moitié écrit. Dossiers et chemins sont validés : impossible d'écrire ou
  de supprimer hors du dossier des modèles.
- L'espace disque est vérifié avant chaque téléchargement (marge de 256 Mo).

## Publier une nouvelle version

1. modifier le code dans `portal-aio/` ;
2. construire l'archive (convertit les fins de ligne en LF, reprend les droits
   d'exécution depuis git et vérifie le résultat) :

   ```bash
   python build_portal.py v3.1.8-models.2
   ```

3. la publier :

   ```bash
   gh release create v3.1.8-models.2 dist/instance-portal.tar.gz --title "Portal v3.1.8-models.2"
   ```

4. changer `PORTAL_VERSION` dans le template. Ça vaut pour les **nouvelles**
   instances ; sur une instance existante, lance `update-portal -v v3.1.8-models.2`
   dans un terminal, puis redémarre `instance_portal` dans l'onglet *Supervisor*.

Pour suivre une nouvelle version du portail Vast, reprends `portal-aio/` depuis
[vast-ai/base-image](https://github.com/vast-ai/base-image) et réapplique les
changements listés dans [NOTICE.md](NOTICE.md) : 4 nouveaux fichiers, quelques lignes
dans 3 fichiers existants.

## Tester en local

```bash
python -m venv .venv
```

```bash
.venv/Scripts/python -m pip install -r portal-aio/requirements.txt pytest
```

Sous Windows, retire d'abord `uvloop` de `portal-aio/requirements.txt` (il n'existe
pas pour Windows), sans le committer.

```bash
.venv/Scripts/python -m pytest portal-aio/tests/test_model_manager.py -q
```

```bash
.venv/Scripts/python dev_portal.py
```

`dev_portal.py` lance le vrai portail sur http://localhost:11111/#/models, avec un
faux dossier ComfyUI dans `.dev/` et des stubs pour ce qui n'existe que sur
l'instance (tunnels Cloudflare, config Caddy).

## Licence

Vast.ai Elastic License, voir [LICENSE.md](LICENSE.md) : usage avec Vast.ai.
Les modifications sont listées dans [NOTICE.md](NOTICE.md).

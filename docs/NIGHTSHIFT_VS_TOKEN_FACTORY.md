# Nightshift et Nebius Token Factory

Note de décision (P1). Elle décrit Meterless Nightshift **tel que ce dépôt l’appelle**, puis le compare à Nebius Token Factory. Les faits cités viennent du code, des docs et de l’enregistrement du 25 octobre 2026. Ce qui n’est pas dans le dépôt est marqué **Incertain**.

Aucun secret n’est reproduit ici. Les noms de variables suffisent.

## 1. Périmètre vérifié

| Source | Ce qu’elle établit |
| --- | --- |
| `backend/app/core/generator.py` | Client Nightshift : probe, `chat/completions`, replis, budgets, trace |
| `backend/app/agents/poe_judge.py` | Un appel Nemotron par zone, rôle `judge` |
| `backend/app/pipeline/run_pipeline.py` | Orchestration, brief, routage `retry` |
| `backend/app/core/llm.py`, `backend/app/core/nvidia.py` | Autre entrée Nightshift, par rôle |
| `backend/app/routers/formalities.py` | `POST /poe/verify` |
| `README.md`, `docs/RUNNING.md`, `docs/sponsors/SPONSOR_TECH.md`, `metadata.json` | Contrat produit et enregistrement live |
| `output_examples/01_bermuda.live_nemotron.json` et `.trace.jsonl` | Un run live du 25 oct. 2026 |
| `.env.example` | Noms de variables et valeurs par défaut (placeholders) |

Le mot « lane » n’apparaît pas dans ce code. La section 3 décrit les deux voies qui existent vraiment : la vérification de zone et le juge.

**Incertain — brief de la tâche, absent du client Nightshift.** Token Factory y est décrit comme une API compatible OpenAI, hôte EU `api.tokenfactory.nebius.com`, modèles NVIDIA Nemotron Lightning / Super-VL / Ultra, inférence batch, facturation au token. Ce dépôt ne contient ni cet hôte, ni un client batch, ni un tarif. La seule mention « Token Factory » est dans `infra/vps/README.md` : le simulateur NAVIGUIDE (code hors de ce dépôt, chemin VPS `~/blue-intelligence-map/naviguide-simulator/`) rédige un film avec « Nemotron 3 Super / Token Factory », sous des plafonds journaliers de tokens et la console Nebius. Ce n’est pas le pipeline PoE.

## 2. Ce que Nightshift fait ici

### Client

`generator.generate` (`backend/app/core/generator.py`) parle à un runtime OpenAI-compatible local :

- base par défaut `http://127.0.0.1:8088/v1` (`METERLESS_BASE_URL`, sinon `MODEL_BASE_URL`) ;
- jeton `METERLESS_RUNTIME_TOKEN` (sinon `MODEL_API_KEY`) ; vide, la probe part sans `Authorization` ;
- modèle demandé `METERLESS_MODEL` (sinon `MODEL_NAME`), défaut `meterless/auto`. Le commentaire du module appelle ça la « route/model » Nightshift ;
- en-têtes `meterless-app: blue-intelligence` et `meterless-scope: user`, plus le bearer s’il y a un jeton.

Séquence : `GET {base}/models` (3 s, cache 60 s, une config à la fois), puis `POST {base}/chat/completions`. Corps fixe : `temperature=0`, `stream=false`, `max_tokens=2000`, et `response_format: {type: json_object}` en mode JSON. Le modèle **servi** est `response.model`, recopié tel quel. Un bloc `<think>…</think>` est retiré du contenu. Pas de lecture du champ `usage`.

Budgets : 90 s par fournisseur (une tentative + au plus un retry dans le même budget), 150 s pour tout l’appel, puis repli sample. Ordre : Nightshift, puis NVIDIA direct seulement si `NVIDIA_API_KEY` est posée (`POST https://integrate.api.nvidia.com/v1/chat/completions`, modèle `NEMOTRON_MODEL`, défaut `nvidia/nemotron-3.5-lightning-30b-a3b`, **sans** les en-têtes `meterless-*`), puis `sample_fn` de l’appelant. `SAMPLE_MODE=true` n’appelle aucun fournisseur.

La trace `MeterlessGenerator` (`logs/trace-<run_id>.jsonl`) enregistre `provider`, `requested_model`, `served_model`, `role`, `fallback_reason`. Les valeurs des variables `METERLESS_RUNTIME_TOKEN`, `MODEL_API_KEY` et `NVIDIA_API_KEY` sont retirées de l’aperçu.

### Exécutions concurrentes

Dans **un** run PoE, les appels modèle sont en série (`backend/app/pipeline/run_pipeline.py`, `_run`) :

1. `NemotronJudge.critique` (un appel) ;
2. boucle `retry` au plus `RETRY_LIMIT = 2` (`backend/app/agents/evaluator.py`) — ResearchAgent et EvaluatorAgent, sans nouvel appel Nightshift ;
3. `BriefingAgent.brief` (un second appel).

Le run live Bermudes a donc fait deux POST : juge ~7,3 s, brief ~45 s (`output_examples/01_bermuda.live_nemotron.trace.jsonl`). `RUN_TIMEOUT_S` vaut 300 s pour tout le run.

Plusieurs `generate()` peuvent partir ensemble. `test_probe_cache_ttl_configuration_and_concurrent_calls` (`backend/tests/test_generator.py`) lance trois appels : une seule probe `GET`, le cache tenant 60 s. Le verrou ne couvre que la probe (un verrou par event loop). Les `chat/completions` ne passent pas par un sémaphore. Le chemin NVIDIA NIM, lui, plafonne à 1–4 appels parallèles (`configure_concurrency` dans `backend/app/core/nvidia.py`). Ce plafond n’entoure pas Nightshift.

`POST /run` et `POST /poe/verify` n’ont pas de verrou de run dans `backend/app/pipeline/`. Deux requêtes HTTP peuvent donc appeler Nightshift en même temps. **Incertain :** le quota du runtime local. Ce dépôt ne le code pas.

`infra/agents/run_lots_bi.py` utilise aussi l’identifiant `meterless/auto` (variable `BIM_MODEL`). C’est le modèle du CLI Cursor `agent`, en pile linéaire (un lot après le précédent), pas `generator.py`.

### Voie vérification

`POST /poe/verify` (`backend/app/routers/formalities.py`) lance `run_pipeline` pour une zone. La validation d’un port est le palier du registre Zetaris (`validation == "official"` pour les tiers 1–2). UN/LOCODE ou World Port Index seuls restent `tier_registered`. Le champ `nemotron_status` reprend le fournisseur et le modèle **du brief** (`result.llm`), pas un canal Nightshift séparé.

`POST /poe/seeds/verify` (`backend/app/routers/runs.py`) classe les graines avec `verdict_for_seed` (`backend/app/services/poe_seeds.py`) : règles locales, aucun appel modèle. Le juge LLM des graines est ailleurs : `poe_seed_enrich._judge_llm` appelle `ask_yes_no` avec `hop_if`. Dès qu’une clé NVIDIA est active, `_nvidia_hop` (`backend/app/core/judge.py`) parle à NIM directement et ne passe pas par Nightshift.

### Voie juge

`NemotronJudge` (`backend/app/agents/poe_judge.py`) fait **un** appel par zone, via `generator.generate(..., json_mode=True, role='judge')`. Il ne passe pas `model=`. Le modèle demandé est donc `METERLESS_MODEL` ou `meterless/auto`, le même que le brief.

Il ne lit que les extraits déjà dans `registry.official_evidence` (20 textes max). Réponse attendue par port : `poe` / `not_poe` / `unclear`, une raison, une citation. `excerpt_verified` n’est vrai que si la citation est dans ces textes. Il n’écrit ni palier, ni score, ni preuve. S’il dit `poe` sur un port `flag` et qu’un retry reste permis, il pose `verdict=retry` : le score suivant vient encore de l’évaluateur. Timeout propre : 90 s (`JUDGE_TIMEOUT_S`). Sans texte officiel, en `SAMPLE_MODE`, sans fournisseur, ou si la réponse est inutilisable, le juge est sauté et la trace dit pourquoi.

Le brief (`write_brief`) est l’autre appel : `role='brief'`, sans `model=` non plus. Son `sample_fn` reconstruit le texte à partir des ports déjà décidés.

### Routage

Deux mécanismes distincts.

**Routage runtime Nightshift.** L’appli envoie un nom ; `response.model` peut être un autre. Run live du 25 oct. 2026, `METERLESS_MODEL=nvidia/nemotron-3.5-lightning-30b-a3b` pour les deux appels (`docs/RUNNING.md`, `docs/sponsors/SPONSOR_TECH.md`) :

| Appel | `requested_model` | `served_model` | `provider` |
| --- | --- | --- | --- |
| Juge | `nvidia/nemotron-3.5-lightning-30b-a3b` | `nvidia/nemotron-3.5-super-vl-120b-a12b` | `nightshift` |
| Brief | le même Lightning | `nvidia/nemotron-3-ultra-550b-a55b` | `nightshift` |

L’appli n’a pas choisi Super-VL ni Ultra. Elle a enregistré ce que le runtime a renvoyé. **Incertain :** la règle interne de ce routage. Elle n’est pas dans ce dépôt. Le rôle (`judge` / `brief`) n’est qu’un champ de trace dans `generator._trace` ; `generate` ne s’en sert pas pour choisir le modèle.

**Routage par rôle, autre entrée.** `llm.nightshift_model` (`backend/app/core/llm.py`) : si `METERLESS_MODEL` est posée, elle gagne pour tous les rôles ; sinon le premier identifiant contenant `nemotron` dans `nvidia.CHAINS`. Chaînes (`backend/app/core/nvidia.py`) :

| Rôle | Premier Nemotron |
| --- | --- |
| `filter`, `normalize`, `search_rank` | `nvidia/nemotron-3.5-lightning-30b-a3b` |
| `page` | `nvidia/nemotron-3.5-super-vl-120b-a12b` |
| `extract`, `json` | `nvidia/nemotron-3-super-120b-a12b` |
| `judge` | `nvidia/nemotron-3-ultra-550b-a55b` |

Il n’y a pas de clé `brief` dans `CHAINS`. Le commentaire au-dessus de `NEMOTRON_ULTRA_MODEL` dit « judge, brief », mais `nightshift_model('brief')` retombe sur la chaîne `json`, donc Super 120B, pas Ultra. `write_brief` n’appelle pas `nightshift_model`. `.env.example` pose `METERLESS_MODEL=meterless/auto`, ce qui écrase ces chaînes pour toute complétion qui passe par `_nightshift`.

`ask_json_tracked` tente Nightshift d’abord, puis la chaîne NIM du rôle, puis OpenRouter, puis Claude. `max_tokens` de `ask_json` n’est pas transmis à `generate` (plafond fixe 2000). `extract_ports` tente `extract_ports_nightshift` (`role="extract"`) avant NIM. `complete_json_cascade` et `ask_yes_no` avec `hop_if` ne commencent pas par Nightshift.

## 3. Token Factory peut-il faire la même chose

Comparaison limitée à ce que le client envoie aujourd’hui et à ce que la tâche affirme sur Token Factory. Rien ici n’a été exécuté contre `api.tokenfactory.nebius.com`.

| Besoin du code | Nightshift dans ce dépôt | Token Factory (tâche + trou du dépôt) |
| --- | --- | --- |
| `POST /v1/chat/completions`, JSON objet, `temperature=0`, non stream | Implémenté vers `127.0.0.1:8088` | **Incertain.** La tâche dit API compatible OpenAI. Le dépôt ne prouve pas que cet hôte accepte `response_format: json_object`, renvoie `model`, ni tolère les en-têtes `meterless-*`. |
| Modèle servi = Nemotron Lightning, Super-VL ou Ultra | Oui sur le run du 25 oct., **choisi par le runtime**, pas par l’appli. Identifiants déjà des constantes dans `nvidia.py` | La tâche liste ces trois familles. L’identifiant `nvidia/nemotron-3-super-120b-a12b` (extract / json) n’est pas dans cette liste. **Incertain** qu’il existe chez Token Factory. |
| Route `meterless/auto` qui réécrit le modèle | Défaut du client et de `.env.example`. Comportement observé une fois (Lightning demandé, Super-VL et Ultra servis) | Aucun équivalent dans le dépôt. Envoyer `meterless/auto` à Token Factory n’a pas de sens vérifiable ici. |
| Juge advisory + brief, un run, deux appels série | Oui | Même forme d’appel si le POST passe. Token Factory ne remplace pas Zetaris, le score, H-MEM, ni la vérif d’extrait. |
| Appels concurrents | Possibles côté `asyncio`, sans plafond Nightshift | La tâche ne dit pas qu’un batch remplace des POST concurrents. Le batch n’existe pas dans `generator.py`. |
| Facturation | Aucun champ `usage`, aucun prix. Le produit s’appelle Meterless ; ce dépôt ne mesure pas un coût | Tâche : au token. Le simulateur NAVIGUIDE (autre code) parle déjà de plafonds et de la console Nebius (`infra/vps/README.md`). |
| Repli hors ligne | NVIDIA NIM puis sample, avec timeouts | À coder. Rien ne pointe vers Token Factory aujourd’hui. |
| Preuve EU | L’hôte codé est localhost ; le repli NVIDIA est `integrate.api.nvidia.com` | Tâche : hôte `api.tokenfactory.nebius.com`. Résidence des prompts : **non écrite** dans ce dépôt. |

Sur le run enregistré, le routage Nightshift a servi un modèle plus gros que Lightning (Ultra sur le brief, 45 s). Épingler Lightning chez Token Factory reproduirait la **demande**, pas le **service** observé. Épingler Ultra pour le juge reproduirait `CHAINS["judge"]`, pas le Super-VL réellement servi le 25 oct. Les deux choix sont traçables ; aucun des deux n’est « ce que Nightshift optimise », puisque la règle de routage est hors dépôt.

Le batch (tâche) ne raccourcit pas le flux actuel : une zone, un juge, puis un brief qui dépend des verdicts. Il serait du code nouveau pour un lot de zones, avec le même doute sur `response_format`.

## 4. Contrainte hackathon

Ce dépôt déclare le **GenAI Works Open Agent Hackathon 2026** (`metadata.json`, `docs/SUBMISSION_DECLARATION.md`). Les sponsors branchés dans le code PoE sont Zetaris, Meterless H-MEM, Nightshift et Nemotron servi par Nightshift (`docs/sponsors/SPONSOR_TECH.md`). `README.md` : « Nemotron weights remain under the NVIDIA licence. » Le dépôt ne dit pas que ces poids sont un « modèle ouvert » au sens d’un règlement Nebius.

**Incertain — contrainte donnée par la tâche, pas par le dépôt.** « Le hackathon Nebius exige un appel Token Factory au runtime et un modèle NVIDIA ouvert. » `docs/archives/PLAN_IMPLEMENTATION_FILIERES_CARTO.md` cite `docs/archives/hackathon-nebius-nvidia.md`. Ce fichier n’est pas dans cet worktree. On ne peut pas vérifier le libellé du règlement ici.

Si cette contrainte s’applique telle quelle :

- un run qui ne contacte que `http://127.0.0.1:8088` ne montre pas, dans ce code, d’appel vers `api.tokenfactory.nebius.com` ;
- le code ne montre pas que Nightshift relaie vers Nebius ;
- `provider: "nightshift"` et un `served_model` nemotron prouvent un Nemotron **via Nightshift**, ce qui est le contrat actuel (`test_live_brief_served_by_nemotron`, `test_live_nightshift_nemotron`, tous deux sous `NIGHTSHIFT_LIVE=1`) ;
- le repli `integrate.api.nvidia.com` est un autre hôte, déjà codé, et ne satisfait pas à lui seul un appel Token Factory.

## 5. Recommandation

Garder la voie métier. La vérification reste Zetaris + score déterministe. Le juge reste une critique, une citation, au plus un retry. Token Factory ne doit pas écrire un palier ni un score.

Pour un appel runtime vers Token Factory, le changement tient dans le client, pas dans l’évaluateur.

1. **Fournisseur distinct dans `generator.generate`.** Base et jeton dédiés (ne pas réutiliser en silence `METERLESS_BASE_URL=http://127.0.0.1:8088/v1`). POST `/chat/completions` au même corps que `_complete` (JSON objet, `temperature=0`, `stream=false`, strip `</think>`). Tracer `provider` avec une valeur nouvelle, plus `requested_model` et `served_model`. Garder le repli sample. Ordre proposé, à figer avant de coder : Token Factory quand il est configuré, Nightshift local sinon, NVIDIA NIM si `NVIDIA_API_KEY`, sample en dernier. Les tests `provider == "nightshift"` restent valables pour le chemin local ; un test opt-in séparé couvre l’hôte Token Factory, sur le modèle de `NIGHTSHIFT_LIVE`.

2. **Modèle explicite, jamais `meterless/auto`.** `poe_judge._ask` et `write_brief` doivent passer `model=`. Aligné sur le commentaire de `nvidia.py` et sur `CHAINS` : Ultra (`nvidia/nemotron-3-ultra-550b-a55b`) pour `judge` et pour `brief`. Super-VL pour `page` seulement (lecture PDF, `extract.parse_pdf_with_nemotron_vl`). Lightning pour `filter` / `normalize` / `search_rank`. Ne pas poser `METERLESS_MODEL=meterless/auto` sur ce chemin : `nightshift_model` écrase alors toutes les chaînes. Pour extract / json, trancher l’identifiant `nvidia/nemotron-3-super-120b-a12b` : il n’est pas dans la liste Token Factory de la tâche. Tant que ce n’est pas vérifié chez l’hôte, ces rôles restent sur NIM / OpenRouter, pas sur un id deviné.

3. **Plafond.** Lire `usage` si la réponse le contient, et poser un sémaphore du même genre que `NVIDIA_MAX_CONCURRENCY` (aujourd’hui 1–4, chemin NIM seulement). Le générateur n’a ni l’un ni l’autre. Utile parce que la tâche annonce une facturation au token ; le dépôt, lui, ne donne pas de prix.

4. **Ne pas mélanger les juges.** `ask_yes_no(..., hop_if=...)` (graines, `poe_seed_enrich.py`) saute Nightshift. Un branchement Token Factory limité à `generator.generate` couvre le pipeline PoE (`/poe/verify`, juge, brief) et `ask_json_tracked` / `extract_ports_nightshift`. Il ne couvre pas le juge de graines tant que `_nvidia_hop` reste devant.

5. **Pas de batch pour égaler le flux actuel.** Un batch Token Factory serait un second client, pour un lot de zones indépendantes. Le brief dépend du juge : les deux ne se mettent pas dans le même batch.

6. **Preuve à garder dans la trace.** Un run dont l’événement générateur montre l’hôte Token Factory (sans jeton), un `requested_model` `nvidia/nemotron-…`, et un `served_model` renvoyé par l’API. Le run Bermudes reste la référence Nightshift ; il ne prouve pas Token Factory.

Tant que le règlement Nebius n’est pas dans le dépôt (`hackathon-nebius-nvidia.md` manquant), cette note ne conclut pas que le hackathon GenAI Works exige cet appel. Elle dit seulement quoi coder si l’appel runtime Token Factory devient obligatoire, sans retirer Nightshift du mode local ni le mode sample sans identifiants.

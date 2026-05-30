# Plano de Implementação: Provisionamento Local Assistido de RTMDet até o Cenário Amarelo

Date: 2026-03-28

Objetivo:
- Reduzir o atrito para disponibilizar RTMDet no TopoSync sem entrar em redistribuição, mirror, bundle ou conversão hospedada.
- Ficar estritamente no cenário "amarelo": assistente local, admin-only, explícito, tudo na máquina do usuário.
- Reaproveitar ao máximo o que o repositório já tem em manifests, install jobs, upload manual, status do processing server e catálogo de modelos.

Fora de escopo neste plano:
- bucket/CDN/object storage próprio com checkpoints ou ONNX
- ONNX/checkpoints embutidos em wheel, Docker image, release ou catálogo hospedado
- conversão na nuvem do TopoSync
- cache compartilhado entre usuários
- "1 clique" silencioso sem aceite explícito

## 1. Conclusões da pesquisa de implementação

### 1.1 O fluxo técnico oficial já existe e deve ser seguido

As fontes oficiais convergem para este fluxo:

1. obter config + checkpoint do modelo RTMDet
2. usar `mmdeploy/tools/deploy.py`
3. usar deploy config de `detection_onnxruntime_static.py` para RTMDet de detecção
4. produzir `end2end.onnx`

Isso é bom para o cenário amarelo porque permite:

- usar tooling oficial em vez de reinventar downloader/exporter
- exibir no wizard os links upstream exatos
- manter o processo local e reproduzível

Fontes:
- RTMDet README oficial
- MMDeploy deployment docs para MMDetection

### 1.2 O repositório já tem quase toda a superfície de produto necessária

Hoje o TopoSync já possui:

- manifests formais por modelo
- catálogo por tarefa com status de disponibilidade
- install jobs com progresso
- upload manual de ONNX
- recovery card na UI com modal para "prepare/get/update"
- install de cópia/download quando permitido
- processing server status com catálogo e install capability

Em outras palavras:

- não é preciso criar um "operador RTMDet" público
- o lugar natural disso é o domínio de aquisição/provisionamento da extensão `com.toposync.vision`

### 1.3 O melhor encaixe arquitetural é estender o fluxo atual de install, não criar um subsistema paralelo

O caminho mais pragmático é:

- manter o conceito de catálogo/acquisition/install job
- adicionar um terceiro modo de aquisição, além de `guided_upload` e `auto_download`
- esse novo modo representa "build local assistido"

Nome sugerido:
- `local_build_assisted`

Isso evita:

- duplicar UI de progresso
- inventar outra família de endpoints
- bifurcar o entendimento do usuário sobre "como um modelo fica pronto"

### 1.4 O escopo inicial precisa ser mais estreito que "todo RTMDet"

Recomendação para a primeira entrega amarela:

- apenas `rtmdet_det_tiny`
- `rtmdet_det_small`
- `rtmdet_det_medium`
- apenas detecção, não segmentação
- apenas export para ONNX Runtime estático
- apenas processing server Linux
- apenas admin/owner

Motivos:

- esses 3 modelos já têm provisioning validado no próprio repositório
- o repo já tem hashes esperados do ONNX exportado
- o fluxo de export atual documentado no repo cobre esses artefatos
- RTMDet-Ins pode entrar depois, mas aumenta superfície e custo de validação

### 1.5 O backend recomendado para a primeira versão amarela é builder local em container

Opções avaliadas:

- `host_python`: menos isolamento, mais frágil, depende de toolchain local
- `container_local`: mais reproduzível, desacopla dependências do processo principal

Recomendação:

- v1 amarela com backend `container_local`
- Docker ou Podman no processing server Linux
- imagem do builder contém apenas stack de código e dependências, nunca pesos/ONNX

Razões:

- reproduz o flow validado no repo de forma mais confiável
- reduz poluição do ambiente do processing server
- facilita versionamento de toolchain
- permite cleanup total do workspace temporário

## 2. Decisões de produto para o cenário amarelo

### 2.1 O recurso não deve ser um operador público do grafo

Não recomendo criar algo como:

- `mmdetection.rtmdet_export`
- `rtmdet.provision`

na superfície pública de pipelines.

Melhor:

- ação administrativa da extensão de visão
- disponível no processing server screen e no modal de recovery do modelo
- explícita como "Preparar localmente" ou "Provisionar nesta máquina"

### 2.2 O recurso deve ser admin-only

Regras:

- apenas `owner` e `admin`
- nunca exposto para `member`
- disponível apenas quando o processing server reportar suporte ao builder local

### 2.3 O wizard precisa ser de aceite explícito, não só de conveniência

Antes de iniciar o job:

- mostrar `checkpoint_url`
- mostrar `config_url`
- mostrar `metafile_url`
- mostrar `paper_url`
- mostrar que o download/conversão ocorrerá localmente
- mostrar que o TopoSync não hospedará os artefatos
- exigir checkbox de aceite

Persistir no log do job:

- usuário
- timestamp
- URL upstream aceita
- modelo escolhido
- processing server alvo

## 3. Modelo de suporte inicial

### 3.1 Suporte de plataforma

Fase amarela inicial:

- Linux: suportado
- macOS: não suportado na primeira entrega
- Windows: não suportado na primeira entrega

Observação:
- macOS/Windows podem entrar depois via OrbStack/WSL2, mas isso é uma fase posterior.

### 3.2 Suporte de modelos

Fase amarela inicial:

- `rtmdet_det_tiny`
- `rtmdet_det_small`
- `rtmdet_det_medium`

Depois:

- RTMDet-Ins somente após consolidar o builder e registrar hashes/validação equivalentes

### 3.3 Suporte de deployment

Fase amarela inicial:

- export ONNX Runtime estático
- `--device cpu` no export
- uso do deploy config oficial de detecção

Motivo:
- menor variabilidade
- melhor reprodutibilidade
- evita introduzir TensorRT/CUDA no provisioning

## 4. Mudanças de arquitetura recomendadas

## 4.1 Extender o manifesto, sem relaxar a política de licença

Manter:

- `redistribution_allowed: false`
- `official_build_allowed: false`

Adicionar ao manifesto/acquisition spec campos para build local assistido, por exemplo:

- `mode: local_build_assisted`
- `checkpoint_url`
- `config_url`
- `metafile_url`
- `paper_url`
- `builder_backend`
- `supported_platforms`
- `explicit_consent_required`

Importante:
- `source_url` atual não é suficiente semanticamente, porque hoje ele modela fonte de artefato pronto para copy/download.
- aqui precisamos modelar fonte de checkpoint upstream para build local, não redistribuição first-party.

### 4.2 Reaproveitar o install manager atual com novo `source_kind`

Proposta:

- manter o install manager como orquestrador central
- adicionar `source_kind = "local_build"`
- adicionar fases novas de job:
  - `preflight`
  - `downloading_checkpoint`
  - `exporting_onnx`
  - `verifying_output`
  - `registering_artifact`
  - `cleaning_up`

Isso reduz o retrabalho na UI porque:

- a UI já sabe mostrar progresso
- o processing server status já expõe install job
- o catálogo já conhece `install_supported`/`install_reason`

### 4.3 Builder local como responsabilidade da extensão `vision`

Não empurrar isso para o core do TopoSync.

Responsabilidades da extensão `vision`:

- política de aquisição dos modelos de visão
- metadata upstream
- preflight do builder
- execução do build
- validação do ONNX resultante
- cleanup
- auditoria/proveniência

## 5. Fases do projeto

## Fase 0. Congelar política, escopo e linguagem do produto

Objetivo:
- impedir que a implementação escorregue do amarelo para o vermelho.

Entregas:

- documento de escopo aprovado
- naming aprovado para UI e API
- lista oficial dos modelos suportados na fase amarela
- decisão de plataforma inicial: Linux only
- texto do aviso/aceite explícito revisado

Decisões obrigatórias:

- sem redistribuição
- sem cache compartilhado
- sem nuvem do TopoSync tocando `.pth` ou `end2end.onnx`
- sem bundle em release/image
- sem operador vendor-specific público

Critérios de aceite:

- o time consegue responder, por escrito, "o que este recurso não faz"
- o fluxo amarelo fica distinguido explicitamente do vermelho

## Fase 1. Fundação de metadata e proveniência

Objetivo:
- enriquecer o catálogo para um build local explícito e auditável.

Entregas:

- extensão do schema de `ModelAcquisitionSpec`
- manifests RTMDet detection atualizados com metadata upstream necessária
- campo de suporte de plataforma do builder
- campo de consentimento obrigatório
- estrutura de provenance log no data dir

Detalhes práticos:

- usar como fonte principal o `metafile.yml` e o README oficial
- guardar URLs exatas de checkpoint/config/metafile/paper
- para `tiny/small/medium`, aproveitar os hashes ONNX já documentados no repo

Critérios de aceite:

- o catálogo consegue dizer "este modelo suporta build local assistido"
- a UI já consegue exibir os links upstream e a política sem iniciar build ainda

## Fase 2. Builder local headless no processing server

Objetivo:
- fazer o processing server conseguir preparar o ONNX sozinho, localmente, sem mirror.

Entregas:

- backend `container_local` para export
- preflight de ambiente:
  - Docker/Podman disponível?
  - espaço em disco mínimo?
  - permissões de escrita no destino?
- runner que:
  - cria workspace temporário
  - baixa checkpoint upstream diretamente no host local
  - executa o fluxo oficial de `tools/deploy.py`
  - move `end2end.onnx` para o path esperado do manifest
  - valida sha256 final contra o manifest
  - remove checkpoint e temporários

Recomendação de implementação:

- primeira versão usando container local com imagem pinada do builder
- imagem derivada do fluxo já validado em `docs/VISION_MODEL_PROVISIONING.md`
- mount apenas de:
  - workspace temporário
  - diretório de destino do modelo
  - diretório de logs/proveniência

Se o hash final do ONNX divergir:

- job falha
- artefato não é promovido
- log persiste versões e comandos usados

Critérios de aceite:

- `rtmdet_det_tiny/small/medium` conseguem ser provisionados localmente em Linux
- o arquivo final bate com o hash esperado do manifest
- o processamento não deixa checkpoint/temporários persistidos por padrão

## Fase 3. UI admin-only de "Preparar localmente"

Objetivo:
- transformar o backend headless em fluxo de produto utilizável.

Entregas:

- botão novo no recovery card e/ou processing server screen:
  - `Preparar localmente`
- modal de aceite com:
  - origem upstream
  - links oficiais
  - aviso de build local
  - requisitos da máquina
  - checkbox obrigatório
- progresso detalhado do job
- fallback direto para upload manual quando preflight falhar

Comportamento esperado:

- o botão só aparece para admin/owner
- o botão só aparece se:
  - modelo estiver em `local_build_assisted`
  - processing server suportar builder local
- mensagens de erro precisam ser operacionais, por exemplo:
  - "Docker não encontrado"
  - "Espaço insuficiente"
  - "Falha ao baixar checkpoint upstream"
  - "Export terminou mas o hash do ONNX não confere"

Critérios de aceite:

- admin consegue provisionar `RTMDet Small` sem sair do TopoSync
- usuário comum continua vendo apenas o fluxo seguro já existente
- o fluxo ainda deixa claro que se trata de build local explícito

## Fase 4. Hardening, auditoria e operação

Objetivo:
- tornar o cenário amarelo robusto o suficiente para uso experimental/controlado.

Entregas:

- provenance log por job com:
  - usuário
  - modelo
  - URLs aceitas
  - versões do builder
  - hash do ONNX final
  - timestamps
- retenção/configuração de logs
- lock por modelo/job para evitar builds concorrentes
- cancelamento e retry controlados
- diagnóstico no processing server status:
  - builder suportado?
  - builder backend
  - última execução
  - último erro

Regras operacionais:

- um build por vez por processing server
- sem atualização automática de artefato pronto
- sem rebuild silencioso após upgrade
- upgrade de builder/toolchain deve invalidar explicitamente o cache local de build

Critérios de aceite:

- falhas são recuperáveis
- o operador/admin sabe o que aconteceu sem abrir shell
- o processing server continua funcional mesmo se o builder falhar

## Fase 5. Expansão controlada ainda dentro do amarelo

Objetivo:
- ampliar cobertura sem mudar a postura jurídica.

Expansões possíveis:

- RTMDet-Ins
- support matrix para WSL2/OrbStack
- backend `host_python` como fallback opcional
- seleção de versão do builder por manifest

Pré-condição:

- hashes e fluxo validados por modelo adicional
- UX de consentimento já madura
- operação estável em Linux

## 6. API e modelo de dados recomendados

### 6.1 Mudanças mínimas na API existente

Recomendação:
- estender `POST /api/processing-servers/{server_id}/vision/models/{model_id}/install`

Payload sugerido:

```json
{
  "force": false,
  "mode": "local_build",
  "acknowledge_upstream_terms": true
}
```

Por quê:

- reaproveita surface já existente
- minimiza churn na UI
- mantém semântica de "tornar este modelo disponível nesta máquina"

### 6.2 Campos adicionais no catálogo

Adicionar algo como:

- `builder_supported`
- `builder_backend`
- `builder_platform_reason`
- `upstream_checkpoint_url`
- `upstream_config_url`
- `upstream_metafile_url`
- `upstream_paper_url`
- `consent_required`

## 7. Estratégia de validação

### 7.1 Validação técnica

Para cada modelo inicial:

- build local completo
- hash final do ONNX comparado com manifest
- carga do modelo via ONNX Runtime no processing server
- detecção básica em imagem de teste

### 7.2 Validação de produto

Validar:

- entendimento do usuário admin sobre o aviso
- clareza de erro
- clareza de que tudo fica local
- clareza de que o TopoSync não está hospedando artefatos

### 7.3 Validação de compliance operacional

Checklist:

- logs não capturam conteúdo excessivo de imagem
- checkpoint não fica persistido por padrão
- provenance do build fica registrada
- UI não sugere redistribuição nem endosso upstream

## 8. Riscos principais e mitigação

### 8.1 Fragilidade do toolchain de export

Risco:
- MMDeploy/MMCV/toolchain quebrar por versão/plataforma

Mitigação:
- builder pinado
- Linux only na primeira versão
- hash final obrigatório

### 8.2 Usuário interpretar como "modelo oficialmente redistribuído"

Risco:
- UX comunicar algo mais amplo que o permitido

Mitigação:
- consentimento explícito
- wording cuidadoso
- links upstream visíveis
- sem download automático silencioso

### 8.3 Escopo escorregar para vermelho

Risco:
- "já que temos builder local, vamos subir um mirror"

Mitigação:
- travas de escopo documentadas
- manifests continuam com `redistribution_allowed: false`
- nada de `TOPOSYNC_VISION_OFFICIAL_MODEL_BASE_URL` first-party para RTMDet

### 8.4 Expansão de plataforma cedo demais

Risco:
- tentar Linux, macOS e Windows de uma vez

Mitigação:
- Linux first
- OrbStack/WSL2 só depois

## 9. Ordem recomendada de execução

Sequência prática:

1. Fase 0
2. Fase 1
3. Fase 2 para `rtmdet_det_small` apenas
4. Fase 3 para `rtmdet_det_small`
5. Fase 4
6. ampliar para `tiny` e `medium`
7. só depois avaliar RTMDet-Ins

Motivo:
- `RF-DETR Medium` é o default recomendado no repo
- reduz explosão de matriz de teste
- fecha o ciclo amarelo com o caso mais útil primeiro

## 10. Definição de pronto do cenário amarelo

O cenário amarelo está pronto quando:

- um admin em Linux consegue preparar `RTMDet Small` localmente no processing server
- o TopoSync mostra origem upstream e exige aceite explícito
- o builder usa fluxo oficial e produz `end2end.onnx`
- o ONNX final é validado contra hash esperado
- o arquivo final fica somente na máquina do usuário
- nenhum artefato é hospedado, espelhado ou compartilhado pelo TopoSync
- a UI continua oferecendo upload manual como fallback

## 11. O que fica explicitamente para depois

Não incluir agora:

- RTMDet-Ins na primeira entrega
- Windows/macOS nativos
- Docker image do TopoSync já contendo o builder embutido por default
- múltiplos backends de export
- automação para pesos de terceiros fora do shortlist
- qualquer coisa que transforme o amarelo em delivery hospedado

## 12. Recomendação objetiva

Eu seguiria assim:

1. manter `guided_upload` como oficial estável
2. introduzir `local_build_assisted` como trilha experimental, admin-only
3. implementar builder local em container, Linux only
4. fechar o ciclo completo primeiro com `rtmdet_det_small`
5. só expandir depois de validar UX, hashes, logs e operação

Esse é o menor caminho para diminuir atrito real sem cruzar a linha para redistribuição ou conversão hospedada.

## 13. Fontes de implementação usadas nesta proposta

Oficiais:

- RTMDet README:
  - https://github.com/open-mmlab/mmdetection/blob/main/configs/rtmdet/README.md
  - https://raw.githubusercontent.com/open-mmlab/mmdetection/main/configs/rtmdet/README.md
- RTMDet `metafile.yml`:
  - https://raw.githubusercontent.com/open-mmlab/mmdetection/main/configs/rtmdet/metafile.yml
- MMDeploy para MMDetection:
  - https://mmdeploy.readthedocs.io/en/v1.2.0/04-supported-codebases/mmdet.html

Locais no repositório:

- `extensions/vision/README.md`
- `extensions/vision/manifests/rtmdet_det_small.json`
- `extensions/vision/src/toposync_ext_vision/registry/installer.py`
- `extensions/vision/src/toposync_ext_vision/registry/recommendations.py`
- `frontend/src/ui/screens/pipelines/editor/panels/VisionPanels.tsx`
- `frontend/src/util/api.ts`
- `docs/VISION_MODEL_PROVISIONING.md`
- `docs/PROCESSING_SERVER_DEVELOPMENT.md`

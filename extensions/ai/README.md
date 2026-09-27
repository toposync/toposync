# Toposync AI Extension

First-party extension for AI-assisted pipeline operators.

This first phase focuses on image workflows:

- `ai.smart_crop`: locates an object/region from a natural-language description and crops the frame.
- `ai.condition_filter`: evaluates a natural-language visual condition and emits packets only when it matches.

The extension is designed around local-first usage. Ollama is the default provider target, with
`qwen3.8:27b-mlx` as the initial high-quality local vision recommendation. Cloud providers can be
added through provider profiles and explicit fallback chains.

## Assistente de alteração de pipelines

O painel **Assistente de pipelines** prepara uma proposta para um pipeline existente,
desativado, em modo JSON ou interativo. A proposta usa um perfil Ollama habilitado
em endereço local, passa pelo compilador real e mostra as alterações antes de salvar.
A prévia requer leitura e compilação; aplicar e desfazer requerem permissão de escrita.
Salvar não ativa o pipeline. Alterações concorrentes impedem aplicar ou desfazer.

As propostas ficam na memória por uma hora. Reiniciar o servidor invalida a prévia e
o desfazer. Referências novas de câmera, fonte, modelo ou outro recurso devem ser
configuradas pelo editor; este primeiro fluxo reutiliza as referências e os tipos de
operadores existentes. Isso limita o contexto enviado ao modelo; adicionar tipos de
etapas diferentes ainda exige o editor.
O modelo não executa Python ou comandos. Falha do provedor ou da validação mantém o
pipeline original. O resumo e os detalhes técnicos precisam de revisão humana.

Este fluxo ainda não certifica criação por linguagem natural, descoberta completa de
recursos, qualidade geral do modelo nem respostas narrativas sobre o histórico.

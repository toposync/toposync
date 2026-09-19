# Calibração assistida como fluxo único

Plano aprovado em 18/09/2026. Reutilizar o assistente e os serviços existentes, sem alterar captura mecânica nem apagar trabalho paralelo.

Entrada única: Calibrar câmera. Panorâmica compatível abre diretamente a marcação; rascunho retoma; calibração ativa abre resultado. Sem imagem compatível, preparação no mesmo assistente, com captura iniciada explicitamente. Seis pontos de ajuste e dois de conferência, seguidos de Concluir calibração. Revisão cria rascunho; ativação mantém controle de conflitos e preserva o mapa anterior. Verificação física permanece separada.

Sequência e critérios em todo.md. Verificação com API simulada, testes de navegador e typecheck/build. Sem movimentar câmeras reais. Riscos: rascunhos duplicados, fonte/revisão incompatível, perda de trabalho local e substituição concorrente; tratar nos contratos existentes e testes focados.

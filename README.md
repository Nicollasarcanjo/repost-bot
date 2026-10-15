# Repost Bot: Telegram → Instagram

Você compartilha um post (Instagram, TikTok ou X) com o seu bot no Telegram, ele baixa a mídia
(TikTok sem marca d'água), ajusta para o formato do Instagram e publica na sua conta profissional,
como **post, story ou os dois**. Tudo se configura por um **painel no navegador** ou pelo próprio Telegram.

```
Telegram ──link──▶ bot.py ──▶ yt-dlp / gallery-dl ──▶ ajuste (Pillow + ffmpeg)
                     │                                     │
   painel ◀──────────┘                                     ▼
 localhost:8080           Instagram ◀── API oficial ◀── URL temporária (túnel Cloudflare)
```

**O que ele faz sozinho**
- Post: foto vira post, vídeo vira reel, vários itens viram carrossel (até 10)
- Story: cada imagem/vídeo vira uma story 9:16, com bordas na cor escolhida (vídeos acima de 60 s são cortados)
- Meme alto/largo demais ganha bordas em vez de ser cortado
- Vídeo HEVC/sem áudio/curto é convertido para H.264 + AAC; GIF vira vídeo
- Evita repostar o mesmo link
- Renova o token do Instagram antes de vencer
- Aceita foto/vídeo enviados direto no Telegram (até 20 MB)

---

## 1. Instalar os programas (uma vez)

No **PowerShell**:

```powershell
winget install Python.Python.3.12
winget install Gyan.FFmpeg
winget install Cloudflare.cloudflared
```

Feche e abra o PowerShell e confira: `python --version`, `ffmpeg -version`, `cloudflared --version`.
Coloque esta pasta num lugar fixo, por exemplo `C:\repost-bot`.

## 2. Criar o bot do Telegram

No Telegram, abra o **@BotFather** → `/newbot` → escolha nome e usuário → copie o token.

## 3. Primeira execução

1. Copie `.env.example` para `.env` e preencha só o `TELEGRAM_TOKEN`.
2. Dê dois cliques em `iniciar.bat` (na primeira vez ele instala tudo, demora um pouco).
3. Abra **http://localhost:8080** no navegador. O resto é feito no painel:
   - **Quem pode usar o bot**: mande `/start` para o seu bot; ele responde com o seu ID. Cole no painel.
   - **Conta do Instagram**: cole o token (passo 4).
   - **Acesso para baixar**: envie o `cookies.txt` (passo 5).

## 4. Token do Instagram

Usa a **Instagram API with Instagram Login**, então não precisa de página do Facebook.

1. <https://developers.facebook.com/apps> → **Criar app**.
2. Caso de uso: **Gerenciar mensagens e conteúdo no Instagram**.
3. No app: **Instagram → Configuração da API com login da empresa do Instagram**.
4. Em **Gerar tokens de acesso**, clique em **Adicionar conta**, entre com a conta onde os memes vão
   sair e aceite as permissões (inclui `instagram_business_content_publish`).
5. **Gerar token** → copie → cole no painel em **Conta do Instagram** → **Conectar**.

O app pode ficar em modo de desenvolvimento, porque a conta é sua. Para trocar de conta depois, é
só colar outro token no painel.

## 5. cookies.txt (para baixar do Instagram)

O Instagram bloqueia download sem login. TikTok e a maioria dos vídeos do X funcionam sem cookies.

1. Instale a extensão **Get cookies.txt LOCALLY** no Chrome/Edge.
2. Entre no instagram.com com uma **conta secundária** (não a que publica).
3. Clique na extensão → **Export** → no painel, **Acesso para baixar → Escolher arquivo**.
4. Para fotos do X, exporte os cookies do x.com e cole no final do mesmo arquivo antes de enviar.

Os cookies vencem de tempos em tempos. Quando os links do Instagram começarem a falhar, envie de novo.

---

## Usar

- **Compartilhe** um post com o bot ou cole o link.
- O texto que você escrever junto vira a **legenda**: `https://vm.tiktok.com/xyz segunda-feira é isso`
- Escreva **#story**, **#post** ou **#ambos** na mensagem para escolher o destino só daquele envio.
- Com **"Mostrar prévia e perguntar"** ligado, o bot manda o meme com os botões
  **Post / Story / Post + story / Cancelar**.

### Comandos do Telegram

| Comando | O que faz |
|---|---|
| `/legenda` | Mostra a legenda padrão |
| `/legenda Seu texto` | Troca a legenda padrão (aceita várias linhas) |
| `/legenda limpar` | Apaga a legenda padrão |
| `/hashtags #memes #humor` | Hashtags que entram no final de toda legenda (`/hashtags limpar` apaga) |
| `/destino` | Escolhe o padrão: post, story ou ambos |
| `/pausar` e `/retomar` | Para e volta a publicar |
| `/status` | Conta conectada, posts nas últimas 24h, fila |
| `/painel` | Endereço do painel |

### O painel (http://localhost:8080)

- **Onde publicar**: post, story ou os dois, com a prévia de como vai sair
- **Legenda**: legenda padrão, hashtags fixas e opção de usar a legenda original
- **Funcionamento**: prévia antes de postar, evitar repetidos, cor das bordas, tamanho do carrossel
- **Conta do Instagram**: conta conectada, validade do token, uso da cota de 24h, trocar token
- **Acesso para baixar**: enviar ou remover o `cookies.txt`
- **Quem pode usar o bot**: IDs do Telegram liberados
- **Últimos envios**: miniaturas do que saiu (e do que falhou, com o motivo)

Tudo salva sozinho e vale a partir do próximo link, sem reiniciar.

O painel só abre no próprio PC. Ele escuta apenas em `localhost` e não passa pelo túnel do
Cloudflare. Se quiser uma senha mesmo assim, defina `PANEL_PASSWORD` no `.env`.

## Iniciar junto com o Windows

1. `Win + R` → `shell:startup` → Enter.
2. Botão direito → **Novo → Atalho** → aponte para `C:\repost-bot\iniciar.bat`.

Desative a suspensão do PC (**Configurações → Sistema → Energia → Suspender: Nunca**). Se o bot cair,
o `iniciar.bat` reinicia sozinho em 15 segundos.

---

## Por que tem um túnel

A API do Instagram não recebe o arquivo direto: ela **baixa a mídia de uma URL pública**. O bot sobe
um servidor local que serve só a pasta `data/public` e abre um túnel gratuito do Cloudflare. Cada
arquivo ganha um nome aleatório e é apagado assim que a publicação termina. Com domínio ou servidor
próprio, use `PUBLIC_BASE_URL` no lugar do túnel.

## Limites

- **100 publicações pela API a cada 24h** (carrossel conta como 1).
- Stories não têm legenda pela API.
- Stories de vídeo: até 60 s (o resto é cortado).
- Carrossel: até 10 itens.
- Slideshow de fotos do TikTok vira carrossel de imagens, sem a música.

## Problemas comuns

| Mensagem | O que fazer |
|---|---|
| `Não consegui baixar esse post` (Instagram) | Envie o `cookies.txt` de novo pelo painel. Post privado não dá. |
| `Não consegui baixar` (TikTok/X) | Feche e abra o `iniciar.bat`: ele atualiza o yt-dlp, o que costuma resolver. |
| `Invalid OAuth access token (code 190)` | Gere um token novo e cole no painel. |
| `a Meta recusou a mídia` | Formato incomum. Veja `data/bot.log`. |
| `O post foi publicado, mas a story falhou` | O post está no ar; mande o link de novo com `#story`. |
| `cloudflared não encontrado` | `winget install Cloudflare.cloudflared` e reabra o terminal. |
| Painel não abre | Confira se a janela do `iniciar.bat` está aberta e se a porta 8080 está livre (`PANEL_PORT`). |

Logs: `data/bot.log`.

## Arquivos

| Arquivo | Função |
|---|---|
| `bot.py` | Bot do Telegram, fila, post e story |
| `panel.py` + `panel/index.html` | Painel de configuração |
| `settings.py` | Configurações que mudam sem reiniciar (`data/settings.json`) |
| `history.py` | Histórico de envios |
| `downloader.py` | Download (yt-dlp → gallery-dl) |
| `media.py` | Ajuste de imagem/vídeo, stories e miniaturas |
| `instagram.py` | Publicação, troca e renovação de token |
| `tunnel.py` | Servidor local + túnel Cloudflare |
| `config.py` | Lê o `.env` |

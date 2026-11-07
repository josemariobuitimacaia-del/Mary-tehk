# Mukanda (site estático, grátis e sem espera)
## Publicar na Render (Static Site, não adormece)
1. Envie esta pasta para um repositório no GitHub.
2. Na Render: New > Static Site > escolha o repositório.
3. Build Command: deixe vazio. Publish Directory: `public`.

## Antes de publicar
1. Edite `public/config.js` (preço, banco, IBAN, titular, WhatsApp).
2. Abra `gerar-codigos.html` no navegador, escreva os códigos e cole o resultado em `codigos` no config.js.
3. Troque `SEU-ENDERECO` pelo endereço final do site em `public/index.html`, `public/robots.txt` e `public/sitemap.xml`.
Os ficheiros server.js e package.json já não são necessários.

"""Testes offline do MacTech (sem base de dados nem Meta reais). Corre: python testes_offline.py
Simula sqlalchemy/models/publico/ferramentas/nivel4 e testa a lógica de áudio, imagem, lista, zanga, erros, menu, backup e rotas."""
import os, sys, types, json, hmac, hashlib, dataclasses, base64
from unittest import mock
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.update(GEMINI_API_KEY="fake", ADMIN_API_KEY="admin-fake", WHATSAPP_VERIFY_TOKEN="vt", WHATSAPP_APP_SECRET="seg")
for k in ("GROQ_API_KEY", "HUB_PHONE_NUMBER_ID", "HUB_ACCESS_TOKEN"):
    os.environ.pop(k, None)

# ---- stubs das partes que precisam de SQLAlchemy ----
sa = mock.MagicMock(); exc = types.ModuleType("sqlalchemy.exc")
class IntegrityError(Exception): pass
exc.IntegrityError = IntegrityError
sys.modules.update({"sqlalchemy": sa, "sqlalchemy.exc": exc, "sqlalchemy.orm": mock.MagicMock()})
models = types.ModuleType("models")
class _M(type):  # qualquer coluna (Empresa.no_hub, Mensagem.id...) existe e aceita .isnot(), ==, etc.
    def __getattr__(cls, nome):
        return mock.MagicMock()
for n in ("Conversa", "Departamento", "Empresa", "Mensagem", "Produto", "Encomenda", "Ticket"):
    setattr(models, n, _M(n, (), {}))
models.Base = type("Base", (), {"metadata": mock.MagicMock()}); models.engine = mock.MagicMock()
models.URL_ERRO = None; models.URL_INVALIDA = False
models.SessionLocal = mock.MagicMock(); models.agora = lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
models.descrever_erro_db = lambda e: "erro"; models.init_db = lambda: None
sys.modules["models"] = models
for n in ("publico", "ferramentas", "nivel4"):
    sys.modules[n] = mock.MagicMock()
sys.modules["publico"].INICIO = "<html>MacTech</html>"

import app as A, whatsapp as W, ia as I, planos
import logging; logging.disable(logging.CRITICAL)
ok = lambda n: print("OK      ", n)

# =============== 1. deteção de pedido/zanga ===============
assert A.cliente_zangado("Isto é uma VERGONHA!") and A.cliente_zangado("Péssimo atendimento") and A.cliente_zangado("exijo o meu dinheiro")
for normal in ["Quanto custa o seguro contra roubo?", "Preciso de um advogado para contratos", "Que horas abrem?", "Podem enviar fatura?", "Vocês têm lixo reciclável?"]:
    assert not A.cliente_zangado(normal), normal
assert A.pede_humano("quero falar com uma pessoa")
ok("zanga: apanha sinais claros e não dispara em perguntas normais")

# =============== 2. preparar() ===============
def prep(texto, humano_ativo=True, humano_assumiu=False):
    emp = NS(nome="Loja X", estado="ativo", plano_ate=None, teste_ate=None, humano_ativo=humano_ativo)
    conv = NS(humano_assumiu=humano_assumiu, cliente_numero="244900000000")
    with mock.patch.object(A, "limite_diario_atingido", return_value=False), mock.patch.object(A, "historico_ia", return_value=[{"role": "user", "content": texto}]), \
         mock.patch.object(A, "limite_mensal_atingido", return_value=False), mock.patch.object(A, "humano_sem_resposta", return_value=False):
        return A.preparar(mock.MagicMock(), emp, conv, texto), conv
r, c = prep("isto é uma vergonha"); assert r == ("fixo", A.MSG_TRANSICAO_ZANGADO) and c.humano_assumiu
r, c = prep("isto é uma vergonha", humano_ativo=False); assert r[0] == "ia" and not c.humano_assumiu
r, c = prep("quero falar com um atendente"); assert r == ("fixo", A.MSG_TRANSICAO) and c.humano_assumiu
r, c = prep("quero falar com um atendente", humano_ativo=False); assert r == ("fixo", A.MSG_SEM_HUMANO)
r, c = prep("que horas abrem?"); assert r[0] == "ia"
r, c = prep("olá", humano_assumiu=True); assert r == ("humano", None)
ok("preparar: zangado→humano (só se há equipa); pedido explícito igual a antes; conversa normal→IA")

# =============== 3. whatsapp.py ===============
class Resp:
    def __init__(s, code=200, js=None, headers=None, blocos=None, text=""):
        s.status_code, s._js, s.headers, s._b, s.text = code, js, headers or {}, blocos or [], text
    def json(s): return s._js
    def raise_for_status(s):
        if s.status_code >= 400: raise W.requests.HTTPError(f"{s.status_code}")
    def iter_content(s, n): return iter(s._b)
    def close(s): pass
visto = []
def post(url, **kw): visto.append(kw["json"]); return Resp(200)
with mock.patch.object(W.requests, "post", post):
    W.enviar_texto("t", "P", "244", "olá")
    assert visto[0] == {"messaging_product": "whatsapp", "to": "244", "type": "text", "text": {"body": "olá"}}
    W.enviar_lista("t", "P", "244", "Escolhe", [("1", "Empresa com nome muito comprido de verdade"), ("2", "B")])
    p = visto[1]; assert p["type"] == "interactive" and p["interactive"]["type"] == "list"
    rows = p["interactive"]["action"]["sections"][0]["rows"]
    assert rows[0] == {"id": "1", "title": "Empresa com nome muito comprido de verdade"[:24]} and len(rows[0]["title"]) <= 24 and len(p["interactive"]["action"]["button"]) <= 20
    for mau in ([], [(str(i), "x") for i in range(11)]):
        try: W.enviar_lista("t", "P", "244", "x", mau); assert False
        except ValueError: pass
ok("whatsapp: texto igual ao de antes; lista dentro dos limites da Meta (10 linhas, títulos de 24)")
# erros: 400 não repete; 503 repete 3x
cont = {"n": 0}
def p400(url, **kw): cont["n"] += 1; return Resp(400, text="x")
def p503(url, **kw): cont["n"] += 1; return Resp(503, text="x")
with mock.patch.object(W.requests, "post", p400), mock.patch.object(W.time, "sleep", lambda s: None):
    try: W.enviar_texto("t", "P", "244", "a"); assert False
    except W.requests.HTTPError: assert cont["n"] == 1
cont["n"] = 0
with mock.patch.object(W.requests, "post", p503), mock.patch.object(W.time, "sleep", lambda s: None):
    try: W.enviar_texto("t", "P", "244", "a"); assert False
    except W.requests.HTTPError: assert cont["n"] == 3
ok("whatsapp: erro definitivo não repete; erro temporário repete 3 vezes")
# baixar_midia
def get_ok(url, **kw):
    if url.endswith("/MID"): return Resp(200, js={"url": "https://cdn.exemplo/ficheiro", "mime_type": "audio/ogg", "file_size": 1000})
    return Resp(200, headers={}, blocos=[b"abc", b"def"])
with mock.patch.object(W.requests, "get", get_ok):
    assert W.baixar_midia("t", "MID") == (b"abcdef", "audio/ogg")
with mock.patch.object(W.requests, "get", lambda url, **kw: Resp(200, js={"url": "http://inseguro/x", "file_size": 1})):
    try: W.baixar_midia("t", "MID"); assert False
    except ValueError: pass
with mock.patch.object(W.requests, "get", lambda url, **kw: Resp(200, js={"url": "https://x/y", "file_size": 50_000_000})):
    try: W.baixar_midia("t", "MID"); assert False
    except ValueError: pass
def get_grande(url, **kw):
    if "graph" in url: return Resp(200, js={"url": "https://x/y", "mime_type": "image/jpeg"})
    return Resp(200, blocos=[b"x" * (6 * 1024 * 1024)] * 2)
with mock.patch.object(W.requests, "get", get_grande):
    try: W.baixar_midia("t", "MID"); assert False
    except ValueError: pass
ok("whatsapp: descarrega; recusa endereço http, ficheiro grande (declarado ou real)")

# =============== 4. ia.entender_midia ===============
def gem(texto): return Resp(200, js={"candidates": [{"content": {"parts": [{"text": texto}]}, "finishReason": "STOP"}]})
env = {}
def post_gem(url, **kw): env["corpo"] = kw["json"]; return env["resp"]
with mock.patch.object(I.requests, "post", post_gem):
    env["resp"] = gem("Bom dia, quanto custa o corte?")
    assert I.entender_midia(b"\x00\x01", "audio/ogg; codecs=opus", "audio") == "Bom dia, quanto custa o corte?"
    parte = env["corpo"]["contents"][0]["parts"][0]["inline_data"]
    assert parte["mime_type"] == "audio/ogg" and base64.b64decode(parte["data"]) == b"\x00\x01"
    assert "Transcreve" in env["corpo"]["contents"][0]["parts"][1]["text"]
    env["resp"] = gem("[[ININTELIGIVEL]]"); assert I.entender_midia(b"x", "image/jpeg", "image") == ""
    env["resp"] = gem("Comprovativo de 5000 Kz, não verificado."); assert "Descreve" in (I.entender_midia(b"x", "image/png", "image") and env["corpo"]["contents"][0]["parts"][1]["text"])
    env["resp"] = Resp(403, text="x")
    try: I.entender_midia(b"x", "image/png", "image"); assert False
    except RuntimeError: pass
ok("ia: transcreve/descreve, tira parâmetros do mime, ininteligível→vazio, erro da IA→RuntimeError")
assert "pagamento foi recebido" in I.REGRAS_BASE and "Mensagem de voz do cliente" in I.REGRAS_BASE
ok("ia: regras novas presentes (pagamentos e áudio/imagem como dados)")

# =============== 5. _processar (fluxo do WhatsApp) ===============
def cenario(msg, hub=False, empresas=None, decidir=None, erro_lista=False, baixar=None, entender=None, enviar_texto_erro_a_seguir=False):
    envios, chamadas = [], {}
    emp = NS(id=1, nome="Salão Bela", wa_access_token="tok", estado="ativo", plano_ate=None, teste_ate=None, humano_ativo=True, no_hub=True)
    conv = NS(id=5, empresa_id=None if hub else 1, humano_assumiu=False, inicio_historico_id=0, contacto=None)
    db = mock.MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = None
    db.query.return_value.filter_by.return_value.one_or_none.return_value = emp
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = empresas or []
    db.get.side_effect = lambda modelo, _id: None if modelo is planos.PlanoEmpresa else emp   # empresa sem pacote (conta antiga)
    A.SessionLocal = mock.MagicMock(); A.SessionLocal.return_value.__enter__.return_value = db
    cfg2 = dataclasses.replace(A.cfg, hub_pnid="HUB", hub_token="ht") if hub else A.cfg
    def dec(db_, e, c, texto, mu): chamadas["texto"] = texto; return "Resposta da IA"
    def lista(*a, **k):
        chamadas["lista"] = a
        if erro_lista: raise RuntimeError("Meta recusou")
    with mock.patch.object(A, "cfg", cfg2), mock.patch.object(A, "obter_conversa", return_value=conv), \
         mock.patch.object(A, "guardar", return_value=NS(id=9)), \
         mock.patch.object(A, "decidir_resposta", decidir or dec), \
         mock.patch.object(A.whatsapp, "enviar_texto", lambda t, p, n, x: envios.append(x)), \
         mock.patch.object(A.whatsapp, "enviar_lista", lista), \
         mock.patch.object(A.whatsapp, "baixar_midia", baixar or (lambda t, m: (b"dados", "audio/ogg"))), \
         mock.patch.object(A.ia, "entender_midia", entender or (lambda d, m, t: "")):
        A._processar("HUB" if hub else "PNID", {"from": "244911", "id": "wamid1", **msg})
    return envios, chamadas

e, c = cenario({"type": "text", "text": {"body": "Quanto custa?"}})
assert c["texto"] == "Quanto custa?" and e == ["Resposta da IA"]; ok("texto normal: igual a antes")
e, c = cenario({"type": "audio", "audio": {"id": "MID"}}, entender=lambda d, m, t: "Queria marcar para sexta")
assert c["texto"] == "[Mensagem de voz do cliente] Queria marcar para sexta" and e == ["Resposta da IA"]; ok("áudio: transcrito e respondido como texto")
def falha(t, m): raise RuntimeError("Meta caiu")
e, c = cenario({"type": "audio", "audio": {"id": "MID"}}, baixar=falha)
assert e == [A.MSG_SO_TEXTO] and "texto" not in c; ok("áudio impossível de ler: cliente recebe aviso simples")
e, c = cenario({"type": "audio", "audio": {"id": "MID"}}, entender=lambda d, m, t: "")
assert e == [A.MSG_SO_TEXTO]; ok("áudio ininteligível: aviso simples")
e, c = cenario({"type": "image", "image": {"id": "MID", "caption": "quanto fica isto?"}}, entender=lambda d, m, t: "Foto de um vestido azul")
assert c["texto"] == "[Imagem enviada pelo cliente] Foto de um vestido azul Legenda do cliente: quanto fica isto?"; ok("imagem com legenda: descrição + legenda")
e, c = cenario({"type": "image", "image": {"id": "MID", "caption": "quanto fica isto?"}}, baixar=falha)
assert c["texto"] == "quanto fica isto?"; ok("imagem ilegível mas com legenda: usa a legenda")
e, c = cenario({"type": "image", "image": {"id": "MID"}}, baixar=falha)
assert e == [A.MSG_SO_TEXTO]; ok("imagem ilegível sem legenda: aviso simples")
e, c = cenario({"type": "video", "video": {"id": "V"}}); assert e == [A.MSG_SO_TEXTO]; ok("vídeo: aviso simples (como antes)")
e, c = cenario({"type": "sticker", "sticker": {}}); assert e == [A.MSG_SO_TEXTO]; ok("sticker: aviso simples")
e, c = cenario({"type": "interactive", "interactive": {"type": "list_reply", "list_reply": {"id": "2", "title": "Outra"}}})
assert c["texto"] == "2"; ok("toque numa opção da lista: conta como o número escolhido")
def rebenta(*a): raise ValueError("bug qualquer")
e, c = cenario({"type": "text", "text": {"body": "olá"}}, decidir=rebenta)
assert e == [A.MSG_ERRO]; ok("erro inesperado a meio: cliente recebe mensagem simpática, não o erro técnico")

tres = [NS(id=i, nome=f"Empresa {i}", estado="ativo", plano_ate=None, teste_ate=None, no_hub=True) for i in (1, 2, 3)]
e, c = cenario({"type": "text", "text": {"body": "olá"}}, hub=True, empresas=tres)
assert "lista" in c and [i for i, _ in c["lista"][4]] == ["1", "2", "3"] and e == []; ok("triagem: envia lista tocável")
e, c = cenario({"type": "text", "text": {"body": "olá"}}, hub=True, empresas=tres, erro_lista=True)
assert len(e) == 1 and "Empresa 1" in e[0] and "De que empresa" in e[0]; ok("triagem: se a lista falhar, envia a pergunta em texto")
e, c = cenario({"type": "text", "text": {"body": "olá"}}, hub=True, empresas=tres[:1])
assert "lista" not in c and len(e) == 1; ok("triagem com 1 empresa: só texto")
e, c = cenario({"type": "text", "text": {"body": "2"}}, hub=True, empresas=tres)
assert e and "Empresa 2" in e[0] and "lista" not in c; ok("triagem: responder '2' escolhe a empresa 2")
e, c = cenario({"type": "audio", "audio": {"id": "MID"}}, hub=True, empresas=tres, entender=lambda d, m, t: "queria falar com a Empresa 3")
assert e and "lista" in c or e; ok("triagem por voz não rebenta")

# =============== 6. rotas Flask ===============
cli = A.app.test_client()
A.SessionLocal = mock.MagicMock()
r = cli.get("/saude"); assert r.status_code == 200 and r.get_json()["versao"] == A.VERSAO == "2026-11-01-manutencao", r.data
r = cli.get("/"); assert r.status_code == 200
r = cli.get("/webhook?hub.mode=subscribe&hub.verify_token=vt&hub.challenge=123"); assert r.data == b"123"
r = cli.get("/webhook?hub.mode=subscribe&hub.verify_token=errado&hub.challenge=123"); assert r.status_code == 403
corpo = json.dumps({"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "P"}, "messages": [{"id": "w1", "from": "244", "type": "audio", "audio": {"id": "M"}}]}}]}]}).encode()
assinatura = "sha256=" + hmac.new(b"seg", corpo, hashlib.sha256).hexdigest()
enviados = []
with mock.patch.object(A.executor, "submit", lambda f, *a: enviados.append(a)):
    assert cli.post("/webhook", data=corpo, headers={"X-Hub-Signature-256": "sha256=00"}).status_code == 403
    assert cli.post("/webhook", data=corpo, headers={"X-Hub-Signature-256": assinatura, "Content-Type": "application/json"}).status_code == 200
assert enviados and enviados[0][1]["type"] == "audio"
ok("rotas: /saude mostra a versão, / abre, webhook valida assinatura e aceita áudio")

# =============== 7. comandos para trocar de empresa ===============
for sim in ["mudar de empresa", "Quero trocar de empresa", "OUTRA EMPRESA por favor", "voltar ao menu", "Menu principal"]:
    assert A.quer_trocar_empresa(sim), sim
for nao in ["quero ver o menu", "cancelar", "voltar amanhã", "qual é a empresa?", "preciso de ajuda"]:
    assert not A.quer_trocar_empresa(nao), nao
ok("menu: só frases claras voltam à lista (menu, cancelar e voltar sozinhos não)")
e, c = cenario({"type": "text", "text": {"body": "2"}}, hub=True, empresas=tres)
assert "mudar de empresa" in e[0]; ok("triagem: ao escolher a empresa, explica como trocar")

# =============== 8. cópia de segurança ===============
import datetime as _dt
def linhas(modelo_nome):
    campos = [c for c in A._BACKUP[modelo_nome][1]]
    d = {c: (_dt.datetime(2026, 10, 8, tzinfo=_dt.timezone.utc) if c in ("teste_ate", "plano_ate", "atualizado", "criado") else f"{c}-valor") for c in campos}
    d.update(id=1); return [NS(**d)]
por_modelo = {A._BACKUP[n][0]: linhas(n) for n in A._BACKUP}
por_modelo[planos.PlanoEmpresa] = [NS(empresa_id=1, plano="basico", arquivada_em=None)]
por_modelo[planos.UsoIA] = [NS(empresa_id=1, mes="2026-10", chamadas=3, entrada=100, saida=20)]
dbb = mock.MagicMock()
dbb.query.side_effect = lambda m: NS(order_by=lambda *_: NS(all=lambda: por_modelo[m]), all=lambda: por_modelo[m])
A.SessionLocal = mock.MagicMock(); A.SessionLocal.return_value.__enter__.return_value = dbb
r = cli.get("/api/backup"); assert r.status_code == 401
r = cli.get("/api/backup", headers={"X-API-Key": "admin-fake"}); assert r.status_code == 200, r.data
assert "attachment" in r.headers["Content-Disposition"] and ".json" in r.headers["Content-Disposition"]
dados = json.loads(r.data); assert set(dados["tabelas"]) == {"empresas", "departamentos", "produtos", "encomendas", "tickets", "planos_empresa", "uso_ia"}
assert dados["tabelas"]["planos_empresa"][0]["plano"] == "basico" and dados["tabelas"]["uso_ia"][0]["chamadas"] == 3
assert dados["tabelas"]["empresas"][0]["teste_ate"].startswith("2026-10-08")
for segredo in ("wa_access_token", "integracao_segredo", "chave_hash"):
    assert segredo not in r.data.decode(), segredo
ok("backup: precisa da chave de admin, devolve JSON com as tabelas (incluindo pacotes e uso da IA) e sem tokens nem chaves")


# =============== 9. manutenção: pacotes, limites, prazos, eliminar, análise ===============
import datetime as dt
agora_ = dt.datetime.now(dt.timezone.utc)

# 9.1 pacotes e limites (lógica pura)
assert set(planos.PLANOS) == {"basico", "profissional", "empresarial"}
assert planos.limite_de("basico") == 600 and planos.limite_de(None) is None and planos.limite_de("lixo") is None
assert planos.preco_de("profissional") == 55000 and planos.preco_de(None) == 0
assert planos.plano_valido("basico") == "basico" and planos.plano_valido("x") is None and planos.plano_valido("") is None
assert planos.formatar_kz(120000) == "120.000"
assert [k for k, _ in planos.lista_publica()] == ["basico", "profissional", "empresarial"]
precos = [p["preco_kz"] for p in planos.PLANOS.values()]; assert precos == sorted(precos)
limites = [p["mensagens_mes"] for p in planos.PLANOS.values()]; assert limites == sorted(limites)
assert planos.estado_do_limite(10, None) == "sem_limite" and planos.estado_do_limite(100, 600) == "ok"
assert planos.estado_do_limite(480, 600) == "perto" and planos.estado_do_limite(600, 600) == "perto" and planos.estado_do_limite(601, 600) == "atingido"
ok("pacotes: 3 pacotes com preços e limites crescentes; estados ok / perto (80%) / atingido")

# 9.2 custo e lucro
assert planos.custo_ia_kz(0, 0) == 0 and abs(planos.custo_ia_kz(1000, 1000) - (planos.CUSTO_IA_KZ_MIL_ENTRADA + planos.CUSTO_IA_KZ_MIL_SAIDA)) < 1e-9
tot = planos.analisar([{"receita_kz": 55000, "custo_ia_kz": 1000, "respostas_ia": 200, "ativa": True},
                       {"receita_kz": 0, "custo_ia_kz": 500, "respostas_ia": 100, "ativa": False}], custo_fixo=20000)
assert tot["receita_kz"] == 55000 and tot["lucro_kz"] == 55000 - 1500 - 20000 and tot["empresas_ativas"] == 1 and tot["respostas_ia"] == 300
assert planos.analisar([], custo_fixo=0)["margem_pct"] == 0   # sem receita não divide por zero
ok("análise: receita, custo da IA, custo fixo e lucro certos; sem empresas não rebenta")

# 9.3 o que cada pacote inclui
for plano, whats, ferr in [("basico", False, False), ("profissional", True, False), ("empresarial", True, True), (None, True, True)]:
    with mock.patch.object(planos, "plano_de", return_value=plano):
        assert planos.permite(mock.MagicMock(), 1, "whatsapp") == whats and planos.permite(mock.MagicMock(), 1, "ferramentas") == ferr, plano
ok("pacotes: WhatsApp e ferramentas só nos pacotes que os incluem; contas antigas (sem pacote) mantêm tudo")

# 9.4 limite mensal: corta a IA mas deixa falar com uma pessoa
dbm = mock.MagicMock(); emp9 = NS(id=1, nome="Loja", estado="ativo", plano_ate=None, teste_ate=None, humano_ativo=True)
for usado, plano, esperado in [(600, "basico", False), (601, "basico", True), (50000, None, False), (3001, "profissional", True)]:
    with mock.patch.object(planos, "plano_de", return_value=plano), mock.patch.object(A, "mensagens_do_mes", return_value=usado):
        assert A.limite_mensal_atingido(dbm, emp9) == esperado, (usado, plano)
def prep9(texto, **kw):
    conv = NS(humano_assumiu=False, cliente_numero="244900000000")
    with mock.patch.object(A, "limite_diario_atingido", return_value=False), mock.patch.object(A, "historico_ia", return_value=[]), \
         mock.patch.object(A, "limite_mensal_atingido", return_value=True), mock.patch.object(A, "humano_sem_resposta", return_value=False):
        return A.preparar(mock.MagicMock(), emp9, conv, texto), conv
r, c = prep9("que horas abrem?"); assert r == ("fixo", A.MSG_LIMITE_MES) and not c.humano_assumiu
r, c = prep9("quero falar com uma pessoa"); assert r == ("fixo", A.MSG_TRANSICAO) and c.humano_assumiu
ok("limite mensal: a IA para ao passar o limite, mas o cliente ainda consegue falar com uma pessoa")

# 9.5 pessoa assumiu e ninguém responde: a IA retoma
dbh = mock.MagicMock()
for ultima, esperado in [(agora_ - dt.timedelta(hours=7), True), (agora_ - dt.timedelta(hours=1), False), (agora_.replace(tzinfo=None) - dt.timedelta(hours=9), True), (None, False)]:
    dbh.query.return_value.filter.return_value.order_by.return_value.first.return_value = (ultima,) if ultima else None
    assert A.humano_sem_resposta(dbh, NS(id=3)) == esperado, ultima
conv9 = NS(humano_assumiu=True, cliente_numero="244900000000")
with mock.patch.object(A, "humano_sem_resposta", return_value=True), mock.patch.object(A, "limite_diario_atingido", return_value=False), \
     mock.patch.object(A, "limite_mensal_atingido", return_value=False), mock.patch.object(A, "historico_ia", return_value=[]):
    assert A.preparar(mock.MagicMock(), emp9, conv9, "olá?")[0] == "ia" and conv9.humano_assumiu is False
conv9 = NS(humano_assumiu=True, cliente_numero="244900000000")
with mock.patch.object(A, "humano_sem_resposta", return_value=False):
    assert A.preparar(mock.MagicMock(), emp9, conv9, "olá?") == ("humano", None) and conv9.humano_assumiu is True
ok("conversa humana abandonada: passadas 6 h sem resposta a IA retoma; com a equipa a responder, continua humana")

# 9.6 contas antigas sem data de fim ganham prazo (uma vez)
antigas = [NS(nome="Velha 1", plano_ate=None), NS(nome="Velha 2", plano_ate=None)]
dbp = mock.MagicMock(); dbp.query.return_value.filter.return_value.all.return_value = antigas
A.SessionLocal = mock.MagicMock(); A.SessionLocal.return_value.__enter__.return_value = dbp
A.dar_prazo_a_contas_antigas()
assert all(24 * 29 < (e.plano_ate - agora_).total_seconds() / 3600 < 24 * 31 for e in antigas) and dbp.commit.called
dbp.query.return_value.filter.return_value.all.return_value = []; dbp.commit.reset_mock(); A.dar_prazo_a_contas_antigas(); assert not dbp.commit.called
A.SessionLocal.side_effect = RuntimeError("base em baixo"); A.dar_prazo_a_contas_antigas()   # não pode rebentar
A.SessionLocal = mock.MagicMock()
ok("prazos: contas ativas sem data ganham 30 dias; sem contas antigas não mexe; base em baixo não derruba nada")

# 9.7 eliminar uma empresa: base de dados real (sqlite) com chaves estrangeiras ligadas
import sqlite3
con = sqlite3.connect(":memory:"); con.execute("PRAGMA foreign_keys = ON")
con.executescript("""
CREATE TABLE empresas(id INTEGER PRIMARY KEY, nome TEXT);
CREATE TABLE conversas(id INTEGER PRIMARY KEY, empresa_id INTEGER REFERENCES empresas(id));
CREATE TABLE mensagens(id INTEGER PRIMARY KEY, conversa_id INTEGER REFERENCES conversas(id));
CREATE TABLE produtos(id INTEGER PRIMARY KEY, empresa_id INTEGER REFERENCES empresas(id));
CREATE TABLE tickets(id INTEGER PRIMARY KEY, empresa_id INTEGER REFERENCES empresas(id), conversa_id INTEGER REFERENCES conversas(id));
CREATE TABLE logos_empresa(empresa_id INTEGER PRIMARY KEY REFERENCES empresas(id), dados TEXT);
CREATE TABLE planos_empresa(empresa_id INTEGER PRIMARY KEY REFERENCES empresas(id), plano TEXT);
INSERT INTO empresas VALUES (1,'A'),(2,'B');
INSERT INTO conversas VALUES (10,1),(11,1),(20,2);
INSERT INTO mensagens VALUES (100,10),(101,10),(102,11),(200,20);
INSERT INTO produtos VALUES (1000,1),(2000,2);
INSERT INTO tickets VALUES (5,1,10),(6,2,20);
INSERT INTO logos_empresa VALUES (1,'x'),(2,'y');
INSERT INTO planos_empresa VALUES (1,'basico'),(2,'basico');
""")
def tab(nome, pk, fks):
    return NS(name=nome, primary_key=NS(columns=[NS(name=pk)]), foreign_keys=[NS(parent=NS(name=c), column=NS(table=NS(name=pt), name=pc)) for c, pt, pc in fks])
meta = NS(sorted_tables=[tab("empresas", "id", []), tab("conversas", "id", [("empresa_id", "empresas", "id")]), tab("mensagens", "id", [("conversa_id", "conversas", "id")]),
                         tab("produtos", "id", [("empresa_id", "empresas", "id")]),
                         tab("tickets", "id", [("empresa_id", "empresas", "id"), ("conversa_id", "conversas", "id")]),
                         tab("logos_empresa", "empresa_id", [("empresa_id", "empresas", "id")]), tab("planos_empresa", "empresa_id", [("empresa_id", "empresas", "id")])])
class DBFalso:
    def execute(self, sql, params): return con.execute(sql, params)
with mock.patch.object(planos, "text", lambda s: s):
    apagadas = planos.eliminar_dados_empresa(DBFalso(), meta, "empresas", 1)
cont = lambda tb, where="1=1": con.execute(f"SELECT COUNT(*) FROM {tb} WHERE {where}").fetchone()[0]
assert cont("empresas", "id=1") == 0 and cont("conversas", "empresa_id=1") == 0 and cont("tickets", "empresa_id=1") == 0
assert cont("mensagens") == 1 and cont("conversas") == 1 and cont("produtos") == 1 and cont("tickets") == 1 and cont("logos_empresa") == 1 and cont("planos_empresa") == 1
assert cont("empresas", "id=2") == 1 and apagadas["mensagens"] == 3 and apagadas["empresas"] == 1
ok("eliminar empresa: apaga a empresa e tudo o que depende dela (mensagens, tickets, logótipo...) e deixa as outras intactas")

# 9.8 o painel tem os dois botões, a análise, os pacotes e o arquivo
import assets
pj, ph, mh = assets.ASSETS["painel.js"][1], assets.ASSETS["painel.html"][1], assets.ASSETS["minha.html"][1]
for texto in ('id="v-cad"', 'id="v-ana"', 'id="sec-analise"', 'id="ep"'):
    assert texto in ph, texto
for texto in ("/api/analise", "/api/pacotes", "/arquivar", "/restaurar", "confirmar:", "'/plano'"):
    assert texto in pj or texto.strip("'") in pj, texto
assert 'id="pc"' in mh and "pacote" in assets.ASSETS["minha.js"][1]
ok("painel: botões «Cadastrar empresas» e «Análise», escolha de pacote, arquivar/restaurar/eliminar e linha do pacote em Minha empresa")

# painel tem o botão e o JS correspondente
import assets
assert 'id="backup"' in assets.ASSETS["painel.html"][1] and "/api/backup" in assets.ASSETS["painel.js"][1]
ok("painel: botão e código do backup presentes")
print("\nTODOS OS TESTES PASSARAM")

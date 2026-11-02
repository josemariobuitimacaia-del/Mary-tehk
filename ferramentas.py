"""Nível 4: ferramentas que a IA pode usar durante a conversa.

- Stock e encomendas: lê o catálogo da MacTech OU, se a empresa ligou o seu CRM/ERP, pergunta ao sistema dela.
- Fatura: NUNCA emite documentos fiscais (em Angola isso exige software certificado). Regista o pedido para o
  Financeiro, ou pede ao ERP da empresa (que é quem emite).
- Departamentos: encaminha para Vendas, Suporte, Financeiro... e passa a conversa a uma pessoa.

A verificação de quem é o dono de uma encomenda é feita AQUI, em código, nunca pela IA."""
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests

from models import Departamento, Empresa, Conversa, Encomenda, Produto, SessionLocal, Ticket
from segredos import decifrar

log = logging.getLogger("mactech.ferramentas")

TODAS = ("stock", "encomendas", "fatura", "departamentos")
CONTACTO_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+|\+?\d[\d \-]{7,}\d")


def norm_contacto(c: str) -> str:
    """Email em minúsculas; telefone só com os últimos 9 dígitos (+244 923 000 000 == 923000000)."""
    c = (c or "").strip().lower()
    if "@" in c:
        return c
    d = re.sub(r"\D", "", c)
    return d[-9:] if len(d) >= 9 else d


def contactos_em(textos) -> set:
    out = set()
    for t in textos:
        for m in CONTACTO_RE.finditer(t or ""):
            n = norm_contacto(m.group(0))
            if n:
                out.add(n)
    return out


@dataclass
class Contexto:
    empresa_id: int
    conversa_id: int
    contactos: set = field(default_factory=set)   # contactos do cliente (telefone do WhatsApp ou o que escreveu)
    verificado: bool = False                      # True = o número vem do WhatsApp (não pode ser falsificado)


# ---------- limite de consultas (contra quem tenta adivinhar códigos de encomenda) ----------
_tent, _trinco = {}, threading.Lock()


def _limite(chave, maximo=8, janela=3600) -> bool:
    t = time.time()
    with _trinco:
        if len(_tent) > 5000:
            for k in [k for k, v in _tent.items() if not v or t - v[-1] > janela]:
                del _tent[k]
        rec = [x for x in _tent.get(chave, []) if t - x < janela]
        if len(rec) >= maximo:
            _tent[chave] = rec
            return True
        rec.append(t)
        _tent[chave] = rec
        return False


# ---------- ligação segura ao sistema da empresa ----------
def url_segura(url: str) -> bool:
    """Só https e só para endereços públicos (impede que alguém aponte para a rede interna do servidor)."""
    try:
        p = urlparse(url or "")
        if p.scheme != "https" or not p.hostname:
            return False
        for info in socket.getaddrinfo(p.hostname, p.port or 443, proto=socket.IPPROTO_TCP):
            ip = ipaddress.ip_address(info[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                    or ip.is_multicast or ip.is_unspecified):
                return False
        return True
    except (OSError, ValueError):
        return False


def assinar(segredo: str, corpo: bytes) -> str:
    return "sha256=" + hmac.new(segredo.encode(), corpo, hashlib.sha256).hexdigest()


def chamar_externo(empresa: Empresa, acao: str, dados: dict, ctx: Contexto) -> dict:
    """POST assinado (HMAC) para o endpoint da empresa. A empresa responde com JSON."""
    if not url_segura(empresa.integracao_url):
        log.warning("Integração de %s recusada (URL não segura ou inacessível)", empresa.nome)
        return {"erro": "Ligação ao sistema da empresa indisponível."}
    corpo = json.dumps({"acao": acao, "dados": dados, "empresa": empresa.slug, "cliente": sorted(ctx.contactos),
                        "identidade_verificada": ctx.verificado, "ts": int(time.time())},
                       ensure_ascii=False).encode()
    cab = {"Content-Type": "application/json",
           "X-MacTech-Assinatura": assinar(decifrar(empresa.integracao_segredo or ""), corpo)}
    try:
        r = requests.post(empresa.integracao_url, data=corpo, headers=cab, timeout=8, allow_redirects=False, stream=True)
        bruto = r.raw.read(20001, decode_content=True)
        r.close()
        if r.status_code != 200:
            return {"erro": f"O sistema da empresa respondeu {r.status_code}."}
        d = json.loads(bruto.decode("utf-8", "replace"))
        return d if isinstance(d, dict) else {"resultado": d}
    except (requests.RequestException, ValueError):
        log.warning("Falha ao chamar a integração de %s", empresa.nome, exc_info=True)
        return {"erro": "Não foi possível consultar o sistema da empresa agora."}


# ---------- esquemas para a IA ----------
def ativas(empresa) -> set:
    return {x for x in (empresa.ferramentas or "").split(",") if x in TODAS}


def esquemas(empresa, departamentos) -> list:
    on, out = ativas(empresa), []
    if "stock" in on:
        out.append({"name": "consultar_stock",
                    "description": "Consulta preço e quantidade em stock de um produto pelo nome ou referência.",
                    "input_schema": {"type": "object", "properties": {"termo": {"type": "string"}}, "required": ["termo"]}})
    if "encomendas" in on:
        out.append({"name": "consultar_encomenda",
                    "description": "Estado de uma encomenda. Pede ao cliente o código e um telefone ou email antes de chamar.",
                    "input_schema": {"type": "object", "properties": {"codigo": {"type": "string"}}, "required": ["codigo"]}})
    if "fatura" in on:
        out.append({"name": "pedir_fatura",
                    "description": "Regista o pedido de fatura de uma encomenda (a equipa financeira envia). Não emite a fatura.",
                    "input_schema": {"type": "object", "properties": {"codigo_encomenda": {"type": "string"}},
                                     "required": ["codigo_encomenda"]}})
    if "departamentos" in on and departamentos:
        nomes = [d.nome for d in departamentos]
        out.append({"name": "transferir_para_departamento",
                    "description": "Passa a conversa a uma pessoa do departamento certo (reclamações, casos complexos, pedido do cliente).",
                    "input_schema": {"type": "object", "properties": {
                        "departamento": {"type": "string", "enum": nomes},
                        "resumo": {"type": "string", "description": "Resumo do caso em 1 ou 2 frases"}},
                        "required": ["departamento", "resumo"]}})
        out.append({"name": "abrir_ticket",
                    "description": "Regista um pedido para a equipa tratar mais tarde, sem passar a conversa a uma pessoa agora.",
                    "input_schema": {"type": "object", "properties": {
                        "departamento": {"type": "string", "enum": nomes}, "assunto": {"type": "string"}},
                        "required": ["departamento", "assunto"]}})
    return out


def bloco_departamentos(departamentos) -> str:
    if not departamentos:
        return ""
    linhas = "\n".join(f"- {d.nome}" + (f": {d.descricao}" if d.descricao else "") for d in departamentos)
    return f"\n\nDepartamentos da empresa (para encaminhar o cliente):\n{linhas}"


# ---------- execução ----------
def criar_ticket(db, empresa_id, conversa_id, departamento, assunto, tipo="geral"):
    t = Ticket(empresa_id=empresa_id, conversa_id=conversa_id, departamento=(departamento or None) and departamento[:60],
               tipo=tipo, assunto=(assunto or "")[:1000])
    db.add(t)
    return t


def _achar_dep(db, empresa_id, nome):
    nome = (nome or "").strip().lower()
    for d in db.query(Departamento).filter_by(empresa_id=empresa_id).all():
        if d.nome.strip().lower() == nome:
            return d
    return None


def _encomenda_do_cliente(db, ctx, codigo):
    """Só devolve a encomenda se o contacto do cliente coincidir com o registado. Erro igual para
    'não existe' e 'não é tua', para não revelar que um código existe."""
    if _limite(("enc", ctx.conversa_id)):
        return None, "Demasiadas consultas seguidas. Tenta mais tarde ou fala com a equipa."
    e = db.query(Encomenda).filter_by(empresa_id=ctx.empresa_id, codigo=(codigo or "").strip().upper()[:40]).one_or_none()
    if not e or norm_contacto(e.contacto) not in ctx.contactos:
        return None, "Não encontrei uma encomenda com esse código associada ao teu contacto. Confirma o código e o telefone ou email."
    return e, None


def _json(d) -> str:
    return json.dumps(d, ensure_ascii=False, default=str)[:3000]


def executar(ctx: Contexto, nome: str, args: dict) -> str:
    """Executa uma ferramenta pedida pela IA e devolve JSON (texto) com o resultado."""
    if not isinstance(args, dict):
        args = {}
    with SessionLocal() as db:
        empresa = db.get(Empresa, ctx.empresa_id)
        if not empresa:
            return _json({"erro": "empresa não encontrada"})
        on = ativas(empresa)
        externo = bool(empresa.integracao_url)
        log.info("Ferramenta %s (empresa %s, conversa %s)", nome, empresa.id, ctx.conversa_id)

        if nome == "consultar_stock" and "stock" in on:
            termo = str(args.get("termo", "")).strip()[:80]
            if externo:
                return _json(chamar_externo(empresa, "consultar_stock", {"termo": termo}, ctx))
            limpo = termo.replace("%", "").replace("_", "")
            if not limpo:
                return _json({"erro": "diz o nome do produto"})
            q = db.query(Produto).filter(Produto.empresa_id == empresa.id,
                                         (Produto.nome.ilike(f"%{limpo}%")) | (Produto.sku == termo.upper())).limit(5).all()
            if not q:
                return _json({"resultado": "Nenhum produto encontrado com esse nome."})
            return _json({"produtos": [{"referencia": p.sku, "nome": p.nome, "preco": p.preco,
                                        "em_stock": p.stock > 0, "quantidade": p.stock} for p in q]})

        if nome == "consultar_encomenda" and "encomendas" in on:
            codigo = str(args.get("codigo", "")).strip()
            if externo:
                return _json(chamar_externo(empresa, "consultar_encomenda", {"codigo": codigo}, ctx))
            e, erro = _encomenda_do_cliente(db, ctx, codigo)
            if erro:
                return _json({"erro": erro})
            return _json({"codigo": e.codigo, "estado": e.estado, "itens": e.itens, "total": e.total,
                          "atualizado": e.atualizado})

        if nome == "pedir_fatura" and "fatura" in on:
            codigo = str(args.get("codigo_encomenda", "")).strip()
            if externo:   # é o ERP da empresa que emite (software certificado)
                return _json(chamar_externo(empresa, "pedir_fatura", {"codigo": codigo}, ctx))
            e, erro = _encomenda_do_cliente(db, ctx, codigo)
            if erro:
                return _json({"erro": erro})
            dep = next((d.nome for d in db.query(Departamento).filter_by(empresa_id=empresa.id).all()
                        if d.nome.lower().startswith("financ")), None)
            criar_ticket(db, empresa.id, ctx.conversa_id, dep, f"Pedido de fatura da encomenda {e.codigo}", "fatura")
            db.commit()
            return _json({"ok": True, "mensagem": "Pedido registado. A equipa financeira envia a fatura ao cliente. "
                                                  "Não digas que a fatura já foi emitida."})

        if nome in ("transferir_para_departamento", "abrir_ticket") and "departamentos" in on:
            dep = _achar_dep(db, empresa.id, args.get("departamento"))
            if not dep:
                return _json({"erro": "departamento inexistente"})
            if nome == "abrir_ticket":
                criar_ticket(db, empresa.id, ctx.conversa_id, dep.nome, str(args.get("assunto", "")), "geral")
                db.commit()
                return _json({"ok": True, "mensagem": f"Pedido registado para {dep.nome}."})
            resumo = str(args.get("resumo", ""))
            criar_ticket(db, empresa.id, ctx.conversa_id, dep.nome, resumo, "transferencia")
            if empresa.humano_ativo is False:
                db.commit()
                return _json({"ok": False, "mensagem": "A equipa não está disponível agora; o pedido ficou registado e "
                                                       "entram em contacto assim que possível."})
            c = db.get(Conversa, ctx.conversa_id)
            c.departamento_id, c.humano_assumiu, c.agente_id = dep.id, True, None
            db.commit()
            log.warning("[ATENÇÃO] %s: conversa %s passada a %s", empresa.nome, ctx.conversa_id, dep.nome)
            return _json({"ok": True, "mensagem": f"Conversa passada ao departamento {dep.nome}. Avisa o cliente que "
                                                  "uma pessoa responde por aqui."})

        return _json({"erro": "ferramenta indisponível"})

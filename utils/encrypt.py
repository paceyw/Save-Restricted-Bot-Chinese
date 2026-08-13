# crypto_ops.py
import base64 as b64
import os as osy

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes as hsh
from cryptography.hazmat.primitives.ciphers import Cipher as Cp
from cryptography.hazmat.primitives.ciphers import algorithms as alg
from cryptography.hazmat.primitives.ciphers import modes as md
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC as PBK

from config import MASTER_KEY as M1, IV_KEY as I1


def dyk(pwd=M1, slt=I1, l=16, *, salt=None):
    if salt is not None:
        slt = salt
    pw = pwd.encode() if isinstance(pwd, str) else pwd
    sl = slt.encode() if isinstance(slt, str) else slt
    kdf = PBK(
        algorithm=hsh.SHA256(),
        length=l,
        salt=sl,
        iterations=100000,
    )
    return kdf.derive(pw)


def ecs(s):
    salt = osy.urandom(16)
    k = dyk(salt=salt)
    n = osy.urandom(12)
    cp = Cp(alg.AES(k), md.GCM(n))
    enc = cp.encryptor()
    p = s.encode()
    ct = enc.update(p) + enc.finalize()
    tg = enc.tag
    encd = b64.b64encode(salt + n + tg + ct).decode()
    return encd


def dcs(ed):
    dat = b64.b64decode(ed.encode())
    try:
        k = dyk(salt=dat[:16])
        n = dat[16:28]
        tg = dat[28:44]
        ct = dat[44:]
        cp = Cp(alg.AES(k), md.GCM(n, tg))
        dec = cp.decryptor()
        res = dec.update(ct) + dec.finalize()
        return res.decode()
    except (InvalidTag, ValueError):
        k = dyk()
        n = dat[:12]
        tg = dat[12:28]
        ct = dat[28:]
        cp = Cp(alg.AES(k), md.GCM(n, tg))
        dec = cp.decryptor()
        res = dec.update(ct) + dec.finalize()
        return res.decode()

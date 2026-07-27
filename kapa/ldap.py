#!/usr/bin/env python3
"""Active Directory (LDAP) — Simple Bind, memberOf, Attribut- und
Gruppen-Mitglieder-Abfrage, nur mit der Standardbibliothek (eigener
minimaler BER/DER-Kodierer). Aus aria_kapa.py ausgelagert (v3.0)."""

import ssl
import urllib.parse


def ldap_bind(url, username, password, timeout=10, insecure=False):
    """Minimaler LDAP Simple Bind (RFC 4511) nur mit der Standardbibliothek.

    Gibt True zurück, wenn sich der Benutzer (UPN, z. B. user@firma.local)
    mit dem Passwort am AD anmelden kann. ldaps:// wird empfohlen; bei
    ldap:// geht das Passwort unverschlüsselt über das Netz."""
    import socket
    if not password:
        return False  # leeres Passwort wäre ein anonymer Bind (immer "Erfolg")
    u = urllib.parse.urlparse(url if "//" in url else "ldap://" + url)
    host = u.hostname
    port = u.port or (636 if u.scheme == "ldaps" else 389)

    def ber_len(n):
        if n < 0x80:
            return bytes([n])
        b = n.to_bytes((n.bit_length() + 7) // 8, "big")
        return bytes([0x80 | len(b)]) + b

    def tlv(tag, payload):
        return bytes([tag]) + ber_len(len(payload)) + payload

    # LDAPMessage { messageID=1, BindRequest { version=3, name, simple-Passwort } }
    bind_req = tlv(0x60, tlv(0x02, b"\x03")
                   + tlv(0x04, username.encode())
                   + tlv(0x80, password.encode()))
    msg = tlv(0x30, tlv(0x02, b"\x01") + bind_req)

    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        if u.scheme == "ldaps":
            ctx = ssl.create_default_context()
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            if insecure:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)
        sock.sendall(msg)
        data = b""
        while len(data) < 12:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    finally:
        sock.close()

    # BindResponse suchen (Tag 0x61), erstes Element darin ist der resultCode
    i = data.find(b"\x61")
    if i < 0 or i + 2 >= len(data):
        raise RuntimeError("Unerwartete LDAP-Antwort")
    j = i + 1
    if data[j] & 0x80:          # lange Längenform überspringen
        j += (data[j] & 0x7F)
    j += 1
    if data[j] != 0x0A:         # ENUMERATED resultCode
        raise RuntimeError("Unerwartete LDAP-Antwort")
    result_code = data[j + 2]
    return result_code == 0


def _ber_len(n):
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _tlv(tag, payload):
    return bytes([tag]) + _ber_len(len(payload)) + payload


def _ber_int(n):
    """Nicht-negativen INTEGER minimal und positiv (kein gesetztes Vorzeichenbit)
    als BER-Wert kodieren."""
    if n <= 0:
        return b"\x00"
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    if b[0] & 0x80:                     # führendes Bit gesetzt -> sonst negativ
        b = b"\x00" + b
    return b


def _read_tlv(sock, buf):
    """Ein vollständiges BER-Element aus dem Socket lesen. Gibt (tag, value,
    restpuffer) zurück."""
    while len(buf) < 2:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("LDAP-Verbindung vorzeitig beendet")
        buf += chunk
    tag = buf[0]
    lb = buf[1]
    idx = 2
    if lb & 0x80:
        num = lb & 0x7F
        while len(buf) < 2 + num:
            buf += sock.recv(4096)
        length = int.from_bytes(buf[2:2 + num], "big")
        idx = 2 + num
    else:
        length = lb
    while len(buf) < idx + length:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("LDAP-Verbindung vorzeitig beendet")
        buf += chunk
    return tag, buf[idx:idx + length], buf[idx + length:]


def _iter_tlv(data):
    """Untergeordnete BER-Elemente einer SEQUENCE/SET durchlaufen."""
    i = 0
    while i + 1 < len(data):
        tag = data[i]
        lb = data[i + 1]
        i += 2
        if lb & 0x80:
            num = lb & 0x7F
            length = int.from_bytes(data[i:i + num], "big")
            i += num
        else:
            length = lb
        yield tag, data[i:i + length]
        i += length


def _dn_to_cn(dn):
    """CN aus einem DN ziehen: 'CN=KapaAdmins,OU=..' -> 'KapaAdmins'."""
    first = dn.split(",")[0].strip()
    if first[:3].upper() == "CN=":
        return first[3:]
    return first


def ldap_member_of(url, bind_dn, bind_pw, base_dn, user_upn,
                   timeout=10, insecure=False):
    """Über ein Service-Konto die AD-Gruppen (CNs) des Benutzers ermitteln.
    Bindet als bind_dn, sucht (userPrincipalName=user_upn) und liest memberOf.
    Gibt die Liste der Gruppen-CNs zurück (leer bei Fehler/keine Gruppen)."""
    import socket
    if not (bind_dn and bind_pw and base_dn and user_upn):
        return []
    u = urllib.parse.urlparse(url if "//" in url else "ldap://" + url)
    host, port = u.hostname, u.port or (636 if u.scheme == "ldaps" else 389)
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        if u.scheme == "ldaps":
            ctx = ssl.create_default_context()
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            if insecure:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)
        # 1) Bind als Service-Konto
        bind_req = _tlv(0x60, _tlv(0x02, b"\x03")
                        + _tlv(0x04, bind_dn.encode())
                        + _tlv(0x80, bind_pw.encode()))
        sock.sendall(_tlv(0x30, _tlv(0x02, b"\x01") + bind_req))
        buf = b""
        tag, val, buf = _read_tlv(sock, buf)       # LDAPMessage (BindResponse)
        # resultCode im BindResponse prüfen
        ok = False
        for t, v in _iter_tlv(val):
            if t == 0x61:                          # BindResponse
                for t2, v2 in _iter_tlv(v):
                    if t2 == 0x0A:
                        ok = (v2[:1] == b"\x00")
                        break
                break
        if not ok:
            return []
        # 2) SearchRequest (messageID 2)
        filt = _tlv(0xA3, _tlv(0x04, b"userPrincipalName")
                    + _tlv(0x04, user_upn.encode()))
        attrs = _tlv(0x30, _tlv(0x04, b"memberOf"))
        search = _tlv(0x63,
                      _tlv(0x04, base_dn.encode())   # baseObject
                      + _tlv(0x0A, b"\x02")          # scope: subtree
                      + _tlv(0x0A, b"\x00")          # derefAliases: never
                      + _tlv(0x02, b"\x00")          # sizeLimit: 0
                      + _tlv(0x02, bytes([timeLimit := 30]))  # timeLimit
                      + _tlv(0x01, b"\x00")          # typesOnly: false
                      + filt + attrs)
        sock.sendall(_tlv(0x30, _tlv(0x02, b"\x02") + search))
        # 3) Antworten lesen bis SearchResultDone (0x65)
        cns = []
        for _ in range(1000):
            tag, val, buf = _read_tlv(sock, buf)    # LDAPMessage
            op = None
            for t, v in _iter_tlv(val):
                if t in (0x64, 0x65):
                    op = (t, v)
                    break
            if not op:
                continue
            if op[0] == 0x65:                        # SearchResultDone
                break
            # SearchResultEntry: objectName, PartialAttributeList
            parts = list(_iter_tlv(op[1]))
            for t, v in parts:
                if t == 0x30:                        # PartialAttributeList
                    for _t, attr in _iter_tlv(v):    # je Attribut
                        sub = list(_iter_tlv(attr))
                        if not sub:
                            continue
                        name = sub[0][1].decode(errors="replace")
                        if name.lower() != "memberof":
                            continue
                        for st, sv in sub[1:]:
                            if st == 0x31:            # SET OF values
                                for vt, vv in _iter_tlv(sv):
                                    cns.append(_dn_to_cn(vv.decode(errors="replace")))
        return cns
    finally:
        sock.close()


def ldap_user_attr(url, bind_dn, bind_pw, base_dn, user_upn, attr,
                   timeout=10, insecure=False):
    """Ein einzelnes AD-Attribut eines Benutzers über das Service-Konto lesen
    (z. B. 'mail', 'proxyAddresses'). Bindet als Service-Konto, sucht
    (userPrincipalName=user_upn) und gibt den ersten Attributwert als String
    zurück ('' bei Fehler/kein Wert)."""
    import socket
    if not (bind_dn and bind_pw and base_dn and user_upn and attr):
        return ""
    u = urllib.parse.urlparse(url if "//" in url else "ldap://" + url)
    host, port = u.hostname, u.port or (636 if u.scheme == "ldaps" else 389)
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        if u.scheme == "ldaps":
            ctx = ssl.create_default_context()
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            if insecure:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)
        bind_req = _tlv(0x60, _tlv(0x02, b"\x03")
                        + _tlv(0x04, bind_dn.encode())
                        + _tlv(0x80, bind_pw.encode()))
        sock.sendall(_tlv(0x30, _tlv(0x02, b"\x01") + bind_req))
        buf = b""
        tag, val, buf = _read_tlv(sock, buf)
        ok = False
        for t, v in _iter_tlv(val):
            if t == 0x61:
                for t2, v2 in _iter_tlv(v):
                    if t2 == 0x0A:
                        ok = (v2[:1] == b"\x00")
                        break
                break
        if not ok:
            return ""
        filt = _tlv(0xA3, _tlv(0x04, b"userPrincipalName")
                    + _tlv(0x04, user_upn.encode()))
        attrs = _tlv(0x30, _tlv(0x04, attr.encode()))
        search = _tlv(0x63,
                      _tlv(0x04, base_dn.encode())
                      + _tlv(0x0A, b"\x02") + _tlv(0x0A, b"\x00")
                      + _tlv(0x02, b"\x00") + _tlv(0x02, bytes([30]))
                      + _tlv(0x01, b"\x00") + filt + attrs)
        sock.sendall(_tlv(0x30, _tlv(0x02, b"\x02") + search))
        for _ in range(1000):
            tag, val, buf = _read_tlv(sock, buf)
            op = None
            for t, v in _iter_tlv(val):
                if t in (0x64, 0x65):
                    op = (t, v)
                    break
            if not op:
                continue
            if op[0] == 0x65:                        # SearchResultDone
                break
            for t, v in _iter_tlv(op[1]):
                if t == 0x30:                        # PartialAttributeList
                    for _t, a in _iter_tlv(v):
                        sub = list(_iter_tlv(a))
                        if not sub:
                            continue
                        if sub[0][1].decode(errors="replace").lower() != attr.lower():
                            continue
                        for st, sv in sub[1:]:
                            if st == 0x31:            # SET OF values
                                for vt, vv in _iter_tlv(sv):
                                    val = vv.decode(errors="replace").strip()
                                    if val:
                                        return val
        return ""
    finally:
        sock.close()


def ldap_group_members(url, bind_dn, bind_pw, base_dn, group_cn,
                       timeout=10, insecure=False, limit=500):
    """Direkte Benutzer-Mitglieder einer AD-Gruppe über das Service-Konto lesen.
    Bindet als Service-Konto, findet die Gruppe per (cn=group_cn) und sucht dann
    alle Benutzer mit (&(objectCategory=person)(memberOf=<GruppenDN>)) — eine
    einzige Suche, unabhängig von der Mitgliederzahl (kein AD-Range-Limit).
    Gibt {ok, error, group_dn, members:[{name,upn,sam}], truncated} zurück.
    Verschachtelte Gruppen werden NICHT aufgelöst (nur direkte Mitglieder)."""
    import socket
    res = {"ok": False, "error": "", "group_dn": "", "members": [], "truncated": False}
    if not (bind_dn and bind_pw and base_dn and group_cn):
        res["error"] = "AD ist nicht vollständig konfiguriert (Service-Konto/Base-DN)."
        return res
    u = urllib.parse.urlparse(url if "//" in url else "ldap://" + url)
    host, port = u.hostname, u.port or (636 if u.scheme == "ldaps" else 389)
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as e:
        res["error"] = f"Keine Verbindung zum AD ({host}:{port}): {e}"
        return res
    try:
        if u.scheme == "ldaps":
            ctx = ssl.create_default_context()
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            if insecure:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)
        # 1) Bind als Service-Konto
        bind_req = _tlv(0x60, _tlv(0x02, b"\x03")
                        + _tlv(0x04, bind_dn.encode())
                        + _tlv(0x80, bind_pw.encode()))
        sock.sendall(_tlv(0x30, _tlv(0x02, b"\x01") + bind_req))
        buf = b""
        tag, val, buf = _read_tlv(sock, buf)
        ok = False
        for t, v in _iter_tlv(val):
            if t == 0x61:
                for t2, v2 in _iter_tlv(v):
                    if t2 == 0x0A:
                        ok = (v2[:1] == b"\x00")
                        break
                break
        if not ok:
            res["error"] = "Bind mit dem Service-Konto fehlgeschlagen (Zugangsdaten?)."
            return res

        def _search(msgid, filt, want_attrs, cap):
            """Eine Suche ausführen; liefert (entries, truncated). Jede entry =
            (objectName, {attr_lower: [werte]})."""
            attrs = _tlv(0x30, b"".join(_tlv(0x04, a.encode()) for a in want_attrs))
            search = _tlv(0x63,
                          _tlv(0x04, base_dn.encode())
                          + _tlv(0x0A, b"\x02")            # scope: subtree
                          + _tlv(0x0A, b"\x00")            # derefAliases: never
                          + _tlv(0x02, _ber_int(cap))    # sizeLimit
                          + _tlv(0x02, bytes([30]))        # timeLimit
                          + _tlv(0x01, b"\x00") + filt + attrs)
            sock.sendall(_tlv(0x30, _tlv(0x02, bytes([msgid])) + search))
            entries, trunc = [], False
            for _ in range(5000):
                _tag, _val, buf2 = _read_tlv(sock, _search.buf)
                _search.buf = buf2
                op = None
                for t, v in _iter_tlv(_val):
                    if t in (0x64, 0x65):
                        op = (t, v)
                        break
                if not op:
                    continue
                if op[0] == 0x65:                          # SearchResultDone
                    for t, v in _iter_tlv(op[1]):
                        if t == 0x0A and v[:1] == b"\x04":  # resultCode 4 = sizeLimitExceeded
                            trunc = True
                        break
                    break
                # SearchResultEntry
                inner = list(_iter_tlv(op[1]))
                obj_name = ""
                amap = {}
                for t, v in inner:
                    if t == 0x04 and not obj_name:
                        obj_name = v.decode(errors="replace")
                    elif t == 0x30:                        # PartialAttributeList
                        for _t, a in _iter_tlv(v):
                            sub = list(_iter_tlv(a))
                            if not sub:
                                continue
                            nm = sub[0][1].decode(errors="replace").lower()
                            vals = []
                            for st, sv in sub[1:]:
                                if st == 0x31:
                                    for _vt, vv in _iter_tlv(sv):
                                        vals.append(vv.decode(errors="replace"))
                            amap[nm] = vals
                entries.append((obj_name, amap))
            return entries, trunc
        _search.buf = buf

        # 1) Gruppe per CN finden -> DN
        gfilt = _tlv(0xA3, _tlv(0x04, b"cn") + _tlv(0x04, group_cn.encode()))
        gents, _ = _search(2, gfilt, ["cn"], 2)
        gents = [e for e in gents if e[0]]
        if not gents:
            res["error"] = f"Gruppe „{group_cn}“ nicht im AD gefunden (unter {base_dn})."
            return res
        group_dn = gents[0][0]
        res["group_dn"] = group_dn

        # 2) direkte Benutzer-Mitglieder holen
        mfilt = _tlv(0xA0,
                     _tlv(0xA3, _tlv(0x04, b"objectCategory") + _tlv(0x04, b"person"))
                     + _tlv(0xA3, _tlv(0x04, b"memberOf") + _tlv(0x04, group_dn.encode())))
        ments, trunc = _search(3, mfilt, ["displayName", "userPrincipalName",
                                          "sAMAccountName"], limit)
        members = []
        for obj_name, amap in ments:
            def first(k):
                return (amap.get(k.lower()) or [""])[0]
            members.append({
                "name": first("displayName") or _dn_to_cn(obj_name),
                "upn": first("userPrincipalName"),
                "sam": first("sAMAccountName"),
            })
        members.sort(key=lambda m: (m["name"] or m["upn"] or "").lower())
        res["members"] = members
        res["truncated"] = trunc or len(members) >= limit
        res["ok"] = True
        return res
    except Exception as e:
        res["error"] = f"AD-Fehler: {e}"
        return res
    finally:
        try:
            sock.close()
        except OSError:
            pass

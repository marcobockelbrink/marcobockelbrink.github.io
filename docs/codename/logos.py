# Erzeugt 7 Sticker-Logos als HTML (transparent gerendert via Chrome).
import os
HTML = os.path.join(os.path.dirname(__file__) or ".", "html")
os.makedirs(HTML, exist_ok=True)

SHELL = """<!doctype html><html><head><meta charset="utf-8"><style>
 html,body{{margin:0;background:transparent}}
 .stage{{width:512px;height:512px;display:flex;align-items:center;justify-content:center;
   font-family:'Segoe UI',Arial,sans-serif}}
 .st{{width:444px;height:444px;box-sizing:border-box;border:11px solid #fff;
   box-shadow:0 16px 36px rgba(0,0,0,.5);display:flex;flex-direction:column;align-items:center;
   justify-content:center;gap:8px;overflow:hidden;position:relative;text-align:center;{st}}}
 .name{{font-weight:800;letter-spacing:.3px;line-height:1}}
 .tag{{font-weight:700;line-height:1.1}}
 .emoji{{line-height:1}}
</style></head><body><div class="stage"><div class="st">{inner}</div></div></body></html>"""

# Tetris-Blöcke
def cell(x, y, c): return f'<rect x="{x}" y="{y}" width="30" height="30" rx="5" fill="{c}"/>'
TETRIS = '<svg width="222" height="150" viewBox="0 0 222 150">' + "".join([
    cell(14,8,'#FACC15'),cell(48,8,'#FACC15'),cell(14,42,'#FACC15'),cell(48,42,'#FACC15'),
    cell(104,8,'#A855F7'),cell(138,8,'#A855F7'),cell(172,8,'#A855F7'),cell(138,42,'#A855F7'),
    cell(14,90,'#22D3EE'),cell(48,90,'#22D3EE'),cell(82,90,'#22D3EE'),cell(116,90,'#22D3EE'),
    cell(158,56,'#34D399'),cell(158,90,'#34D399'),cell(158,124,'#34D399'),cell(192,124,'#34D399'),
]) + '</svg>'

# RAM-Riegel mit Bandana
teeth = "".join(f'<rect x="{x}" y="120" width="6" height="14" fill="#F5C542"/>'
                for x in range(30, 214, 10) if not (112 <= x <= 132))
RAM = f'''<svg width="244" height="150" viewBox="0 0 244 150">
 <polygon points="14,54 40,54 30,150 14,110" fill="#dc2626"/>
 <rect x="24" y="52" width="196" height="74" rx="7" fill="#15803d"/>
 {"".join(f'<rect x="{x}" y="66" width="34" height="32" rx="3" fill="#0f5132"/>' for x in (36,80,124,168))}
 {teeth}
 <rect x="112" y="120" width="20" height="14" fill="#0b0f1e"/>
 <rect x="10" y="46" width="224" height="16" rx="4" fill="#ef4444"/>
</svg>'''

LOGOS = {
 "1_vacancy": ("background:#0a0f1e;border-radius:26%;",
   '<div class="emoji" style="font-size:118px">🛎️</div>'
   '<div class="name" style="color:#34D399;font-size:58px;'
   'text-shadow:0 0 14px rgba(52,211,153,.9),0 0 34px rgba(52,211,153,.55)">vAcancy</div>'
   '<div class="tag" style="color:#7CF3C6;font-size:19px;letter-spacing:3px">★ FREIE KAPAZITÄT ★</div>'),
 "2_platzhirsch": ("background:radial-gradient(circle at 50% 34%,#2f8150,#134228);border-radius:50%;",
   '<div class="emoji" style="font-size:150px">🦌</div>'
   '<div class="name" style="color:#fff;font-size:50px">Platzhirsch</div>'
   '<div class="tag" style="color:#F5C542;font-size:19px">Revierchef im Cluster</div>'),
 "3_clustertetris": ("background:#0d1117;border-radius:26%;",
   TETRIS +
   '<div class="name" style="color:#fff;font-size:37px;font-family:monospace">ClusterTetris</div>'
   '<div class="tag" style="color:#38BDF8;font-size:18px">Passt das noch rein?</div>'),
 "4_rambo": ("background:radial-gradient(circle at 50% 38%,#41512e,#1d2416);border-radius:50%;",
   RAM +
   '<div class="name" style="color:#fff;font-size:56px">RAMbo</div>'
   '<div class="tag" style="color:#F87171;font-size:18px">First Blood beim RAM</div>'),
 "5_overbooked": ("background:linear-gradient(150deg,#6d28d9,#4c1d95);border-radius:26%;",
   '<div class="emoji" style="font-size:104px">✈️</div>'
   '<div class="name" style="color:#ff5a5a;font-size:40px;border:5px solid #ff5a5a;'
   'padding:5px 16px;border-radius:10px;transform:rotate(-7deg)">OVERBOOKED</div>'
   '<div class="tag" style="color:#E9E4FF;font-size:18px;margin-top:10px">VMware sagt: Overcommit</div>'),
 "6_clusterphobie": ("background:radial-gradient(circle at 50% 34%,#8b5cf6,#4c2f9e);border-radius:50%;",
   '<div class="emoji" style="font-size:126px">😰</div>'
   '<div class="name" style="color:#fff;font-size:44px">Clusterphobie</div>'
   '<div class="tag" style="color:#E4DBFF;font-size:18px">Therapie inklusive 🛋️</div>'),
 "7_luftballons": ("background:linear-gradient(160deg,#20456f,#0b1220);border-radius:26%;",
   '<div class="emoji" style="font-size:116px">🎈</div>'
   '<div class="name" style="color:#fff;font-size:42px">99 Luftballons</div>'
   '<div class="tag" style="color:#FF7A7A;font-size:18px">a.k.a. Ballooning</div>'),
}
for key, (st, inner) in LOGOS.items():
    with open(os.path.join(HTML, key + ".html"), "w", encoding="utf-8") as f:
        f.write(SHELL.format(st=st, inner=inner))
print("HTML-Logos:", len(LOGOS))

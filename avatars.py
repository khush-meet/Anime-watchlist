"""Famous anime characters used for profile pictures.

key  = file name in static/avatars/<key>.jpg  (made by `python fetch_avatars.py`)
name = used to look the picture up on AniList / Jikan
"""
AVATARS = [
    dict(key="gojo",     name="Satoru Gojo"),
    dict(key="naruto",   name="Naruto Uzumaki"),
    dict(key="luffy",    name="Monkey D. Luffy"),
    dict(key="goku",     name="Son Goku"),
    dict(key="light",    name="Light Yagami"),
    dict(key="levi",     name="Levi Ackerman"),
    dict(key="itachi",   name="Itachi Uchiha"),
    dict(key="tanjiro",  name="Tanjiro Kamado"),
    dict(key="saitama",  name="Saitama"),
    dict(key="lelouch",  name="Lelouch Lamperouge"),
    dict(key="eren",     name="Eren Yeager"),
    dict(key="killua",   name="Killua Zoldyck"),
    dict(key="zoro",     name="Roronoa Zoro"),
    dict(key="yuji",     name="Yuji Itadori"),
    dict(key="nezuko",   name="Nezuko Kamado"),
    dict(key="edward",   name="Edward Elric"),
    dict(key="kakashi",  name="Kakashi Hatake"),
    dict(key="mikasa",   name="Mikasa Ackerman"),
    dict(key="jinwoo",   name="Sung Jin-Woo"),
    dict(key="anya",     name="Anya Forger"),
    dict(key="sukuna",   name="Ryomen Sukuna"),
    dict(key="sasuke",   name="Sasuke Uchiha"),
]
FREE_POOL = [a["key"] for a in AVATARS]      # free accounts only ever get one of these

# ---- extra options: only Premium users and the admin can pick these ----
PREMIUM_AVATARS = [
    dict(key="gon",        name="Gon Freecss"),
    dict(key="deku",       name="Izuku Midoriya"),
    dict(key="bakugo",     name="Katsuki Bakugo"),
    dict(key="todoroki",   name="Shoto Todoroki"),
    dict(key="zenitsu",    name="Zenitsu Agatsuma"),
    dict(key="inosuke",    name="Inosuke Hashibira"),
    dict(key="rengoku",    name="Kyojuro Rengoku"),
    dict(key="giyu",       name="Giyu Tomioka"),
    dict(key="shinobu",    name="Shinobu Kocho"),
    dict(key="megumi",     name="Megumi Fushiguro"),
    dict(key="nobara",     name="Nobara Kugisaki"),
    dict(key="denji",      name="Denji"),
    dict(key="makima",     name="Makima"),
    dict(key="power",      name="Power"),
    dict(key="vegeta",     name="Vegeta"),
    dict(key="gohan",      name="Son Gohan"),
    dict(key="ichigo",     name="Ichigo Kurosaki"),
    dict(key="rukia",      name="Rukia Kuchiki"),
    dict(key="natsu",      name="Natsu Dragneel"),
    dict(key="spike",      name="Spike Spiegel"),
    dict(key="rem",        name="Rem"),
    dict(key="zerotwo",    name="Zero Two"),
    dict(key="senku",      name="Senku Ishigami"),
    dict(key="kirito",     name="Kirito"),
    dict(key="asuna",      name="Asuna Yuuki"),
    dict(key="kaneki",     name="Ken Kaneki"),
    dict(key="madara",     name="Madara Uchiha"),
    dict(key="hinata",     name="Hinata Hyuga"),
    dict(key="frieren",    name="Frieren"),
    dict(key="yor",        name="Yor Forger"),
    dict(key="loid",       name="Loid Forger"),
    dict(key="mob",        name="Shigeo Kageyama"),
]
BUILTIN = AVATARS + PREMIUM_AVATARS          # everything that has a file in static/avatars/
KEYS = {a["key"] for a in BUILTIN}


def default_avatar(username, pool=None):
    """Free accounts get a stable 'random' famous character based on the username."""
    pool = pool or FREE_POOL
    return pool[sum(ord(c) for c in username) % len(pool)]


"""Client Discord du monitor — application dédiée, dans le même process.

Une application distincte du bot Moddy, avec son propre token et sa propre
connexion gateway : le monitor doit rester capable de parler quand Moddy est
down, ce qui est précisément le cas où on en a besoin.
"""

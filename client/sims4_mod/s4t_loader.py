"""Module racine chargé par le chargeur des scripts Sims 4."""
try:
    from s4t.bootstrap import install
    install()
except Exception as error:
    try:
        from s4t.journal import log
        log('loader', type(error).__name__)
    except Exception:
        # Aucun token ni message d'exception potentiellement sensible.
        print('S4Together: bootstrap indisponible (' + type(error).__name__ + ')')

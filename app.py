"""Point d'entrée d'Uscale. Le code se trouve dans le paquet uscale/."""
from uscale.web import app, main

__all__ = ["app", "main"]

if __name__ == "__main__":
    main()

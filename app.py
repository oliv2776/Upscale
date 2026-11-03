"""Point d'entrée d'Upscale. Le code se trouve dans le paquet upscale/."""
from upscale.web import app, main

__all__ = ["app", "main"]

if __name__ == "__main__":
    main()

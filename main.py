from config import load_config
from app import MediaCleanerApp

if __name__ == "__main__":
    config = load_config()
    MediaCleanerApp(config).run()

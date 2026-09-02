import time
from .config import Config
from .store import Store
from .service import NovelService
from .deepseek import DeepSeekClient


def run_once(store, service):
    job=store.claim_job()
    return service.process(job) if job else None


def main():
    config=Config(); store=Store(config.data_dir/'novel.sqlite3'); service=NovelService(store,DeepSeekClient(config),config)
    # The daemon is long-running, but it only processes explicitly-created jobs;
    # it never creates an unbounded chain of chapters by itself.
    while True:
        run_once(store,service); time.sleep(1)


if __name__=='__main__': main()

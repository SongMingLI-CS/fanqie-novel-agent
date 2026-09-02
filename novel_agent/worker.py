import time
from concurrent.futures import ThreadPoolExecutor
from .config import Config
from .store import Store
from .service import NovelService
from .deepseek import DeepSeekClient


def run_once(store, service):
    job=store.claim_job(service.config.job_timeout)
    return service.process(job) if job else None


def main():
    config=Config(); db_path=config.data_dir/'novel.sqlite3'
    def process_one():
        store=Store(db_path)
        try: return run_once(store,NovelService(store,DeepSeekClient(config),config))
        finally: store.close()
    pool=ThreadPoolExecutor(max_workers=config.worker_concurrency)
    # The daemon is long-running, but it only processes explicitly-created jobs;
    # it never creates an unbounded chain of chapters by itself.
    while True:
        results=[f.result() for f in [pool.submit(process_one) for _ in range(config.worker_concurrency)]]
        result=next((x for x in results if x is not None),None)
        if config.worker_once or result is None and config.worker_once: break
        time.sleep(1)
    pool.shutdown(wait=True)


if __name__=='__main__': main()

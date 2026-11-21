import shutil
from pathlib import Path

import piwigo_downloader as p

c = p.PiwigoClient("https://gallery.example.com/")
info = c.probe()
items = p.collect_images(c, info["categories"], max_images=4)
out = Path("downloads_test")
shutil.rmtree(out, ignore_errors=True)

d = p.Downloader(c, out, threads=4)
stats = d.run(items)
print("done: %s" % stats)
for f in out.rglob("*"):
    if f.is_file():
        print(f, f.stat().st_size)
# 二次运行应全部 skipped
stats2 = d.run(items)
print("rerun: done=%d skipped=%d failed=%d" % (stats2.done, stats2.skipped, stats2.failed))
print("manifest:", d.write_manifest(items, stats2))

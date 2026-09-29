# CI diagnostic only: if main.py hangs, dump where it is stuck after 40s.
import faulthandler
import runpy

faulthandler.dump_traceback_later(40, exit=True)
runpy.run_path("main.py", run_name="__main__")

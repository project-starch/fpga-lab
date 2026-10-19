import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
# lease.py comes from fpga-remote; point FPGA_REMOTE_DIR at a checkout.
os.environ.setdefault('FPGA_REMOTE_DIR', os.path.join(os.path.dirname(HERE), '..', 'wt-lease'))

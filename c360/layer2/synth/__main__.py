"""python -m c360.layer2.synth [--out DIR] [--people N] [--seed S]"""
import argparse

from c360.layer2.synth.generate import generate


def main():
    ap = argparse.ArgumentParser(description="Generate the synthetic Collections 360 world for Layer 2 development.")
    ap.add_argument("--out", help="output directory (default: $C360_WAREHOUSE, else ./warehouse_synth)")
    ap.add_argument("--people", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--questions-out", help="where to write synthetic.yaml (default: c360/layer2/eval/questions/)")
    a = ap.parse_args()
    generate(out=a.out, people=a.people, seed=a.seed, questions_out=a.questions_out)


if __name__ == "__main__":
    main()

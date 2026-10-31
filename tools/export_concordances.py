"""Export compiled fixtures and ID references as chunked Parquet for R, offline."""
import argparse
from pathlib import Path


def main(argv=None):
    import config
    from concordance.parquet import refresh,CHUNK_ROWS,folder,validate
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root',type=Path,default=config.DATA_ROOT)
    parser.add_argument('--dataset-root',type=Path,default=config.DATASET_ROOT)
    parser.add_argument('--output',type=Path,help='R data/concordances folder; defaults to dataset/concordances')
    parser.add_argument('--chunk-rows',type=int,default=CHUNK_ROWS)
    args=parser.parse_args(argv)
    manifest=refresh(args.data_root,args.dataset_root,chunk_rows=args.chunk_rows,output=args.output,report=print)
    if manifest is None:parser.error('No compiled fixture history was found; collect or receive fixtures first')
    target=args.output or folder(args.dataset_root);check=validate(target)
    print(f'Wrote {check["rows"]} fixtures and match/team/player ID tables to {target}')
    print('This exports saved compiled inputs; it does not certify MSI integration or scrape providers.')
    return 0


if __name__=='__main__':raise SystemExit(main())

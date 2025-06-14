from datasets import load_dataset, load_from_disk, Audio, DatasetDict
from multiprocess import set_start_method
from dataspeech import rate_apply, pitch_apply, snr_apply, squim_apply
import torch
import argparse
import os


# We'll use [`main.py`](https://github.com/sawallesalfo/dataspeech/blob/main/main.py) to get the following continuous variables:
# - Speaking rate `(nb_phonemes / utterance_length)`
# - Scale-Invariant Signal-to-Distortion Ratio (SI-SDR)
# - Reverberation


if __name__ == "__main__":
    set_start_method("spawn")
    parser = argparse.ArgumentParser()
    
    # Dataset loading arguments
    parser.add_argument("dataset_name", type=str, help="Path or name of the dataset. See: https://huggingface.co/docs/datasets/v2.17.0/en/package_reference/loading_methods#datasets.load_dataset.path")
    parser.add_argument("--configuration", default=None, type=str, help="Dataset configuration to use, if necessary.")
    parser.add_argument("--from_disk", action="store_true", help="If set, load dataset from disk using load_from_disk instead of load_dataset.")
    
    # S3 storage options
    parser.add_argument("--aws_access_key_id", default=None, type=str, help="AWS access key ID for S3 storage.")
    parser.add_argument("--aws_secret_access_key", default=None, type=str, help="AWS secret access key for S3 storage.")
    parser.add_argument("--aws_endpoint_url", default=None, type=str, help="AWS S3 endpoint URL (e.g., for custom S3-compatible storage).")
    
    # Output arguments
    parser.add_argument("--output_dir", default=None, type=str, help="If specified, save the dataset on disk with this path.")
    parser.add_argument("--repo_id", default=None, type=str, help="If specified, push the dataset to the hub.")
    
    # Column arguments
    parser.add_argument("--audio_column_name", default="audio", type=str, help="Column name of the audio column to be enriched.")
    parser.add_argument("--text_column_name", default="text", type=str, help="Text column name.")
    parser.add_argument("--rename_column", action="store_true", help="If activated, rename audio and text column names to 'audio' and 'text'. Useful if you want to merge datasets afterwards.")
    
    # Processing arguments
    parser.add_argument("--cpu_num_workers", default=1, type=int, help="Number of CPU workers for transformations that don't use GPUs or if no GPU are available.")
    parser.add_argument("--cpu_writer_batch_size", default=1000, type=int, help="writer_batch_size for transformations that don't use GPUs. See: https://huggingface.co/docs/datasets/v2.17.0/en/package_reference/main_classes#datasets.Dataset.map.writer_batch_size")
    parser.add_argument("--batch_size", default=2, type=int, help="This parameters specify how many samples are passed by workers for operations that are using GPUs.")
    parser.add_argument("--penn_batch_size", default=4096, type=int, help="Pitch estimation chunks audio into smaller pieces and processes them in batch. This specify the batch size. If you are using a gpu, pick a batch size that doesn't cause memory errors.")
    parser.add_argument("--num_workers_per_gpu_for_pitch", default=1, type=int, help="Number of workers per GPU for the pitch estimation if GPUs are available. Defaults to 1 if some are avaiable. Useful if you want multiple processes per GPUs to maximise GPU usage.")
    parser.add_argument("--num_workers_per_gpu_for_snr", default=1, type=int, help="Number of workers per GPU for the SNR and reverberation estimation if GPUs are available. Defaults to 1 if some are avaiable. Useful if you want multiple processes per GPUs to maximise GPU usage.")
    parser.add_argument("--apply_squim_quality_estimation", action="store_true", help="If set, will also use torchaudio-squim estimation (SI-SNR, STOI and PESQ).")
    parser.add_argument("--num_workers_per_gpu_for_squim", default=1, type=int, help="Number of workers per GPU for the SI-SNR, STOI and PESQ estimation if GPUs are available. Defaults to 1 if some are avaiable. Useful if you want multiple processes per GPUs to maximise GPU usage.")

    args = parser.parse_args()
    
    # Setup storage options for S3 if provided
    storage_options = None
    if args.aws_access_key_id and args.aws_secret_access_key:
        storage_options = {
            "key": args.aws_access_key_id,
            "secret": args.aws_secret_access_key,
        }
        if args.aws_endpoint_url:
            storage_options["client_kwargs"] = {"endpoint_url": args.aws_endpoint_url}
        print(f"Using S3 storage options{' with custom endpoint: ' + args.aws_endpoint_url if args.aws_endpoint_url else ''}")
    elif args.aws_access_key_id or args.aws_secret_access_key or args.aws_endpoint_url:
        print("Warning: Partial S3 credentials provided. Both --aws_access_key_id and --aws_secret_access_key are required for S3 storage.")
        if not args.aws_access_key_id:
            print("Missing: --aws_access_key_id")
        if not args.aws_secret_access_key:
            print("Missing: --aws_secret_access_key")
    
    if args.from_disk:
        print(f"Loading dataset from disk: {args.dataset_name}")
        if storage_options:
            dataset = load_from_disk(args.dataset_name, storage_options=storage_options)
        else:
            dataset = load_from_disk(args.dataset_name)
    else:
        print(f"Loading dataset from hub/path: {args.dataset_name}")
        if args.configuration:
            dataset = load_dataset(args.dataset_name, args.configuration, num_proc=args.cpu_num_workers)
        else:
            dataset = load_dataset(args.dataset_name, num_proc=args.cpu_num_workers)
        
    audio_column_name = "audio" if args.rename_column else args.audio_column_name
    text_column_name = "text" if args.rename_column else args.text_column_name
    if args.rename_column:
        dataset = dataset.rename_columns({args.audio_column_name: "audio", args.text_column_name: "text"})
        
    dataset = DatasetDict({"train": dataset})

    if args.apply_squim_quality_estimation:
        print("Compute SI-SDR, PESQ, STOI")
        squim_dataset = dataset.map(
            squim_apply,
            batched=True,
            batch_size=args.batch_size,
            with_rank=True if torch.cuda.device_count()>0 else False,
            num_proc=torch.cuda.device_count()*args.num_workers_per_gpu_for_squim if torch.cuda.device_count()>0 else args.cpu_num_workers,
            remove_columns=[audio_column_name], # tricks to avoid rewritting audio
            fn_kwargs={"audio_column_name": audio_column_name,},
        )

    print("Compute pitch")
    pitch_dataset = dataset.cast_column(audio_column_name, Audio(sampling_rate=16_000)).map(
        pitch_apply,
        batched=True,
        batch_size=args.batch_size,
        with_rank=True if torch.cuda.device_count()>0 else False,
        num_proc=torch.cuda.device_count()*args.num_workers_per_gpu_for_pitch if torch.cuda.device_count()>0 else args.cpu_num_workers,
        remove_columns=[audio_column_name], # tricks to avoid rewritting audio
        fn_kwargs={"audio_column_name": audio_column_name, "penn_batch_size": args.penn_batch_size},
    )

    print("Compute snr and reverb")
    snr_dataset = dataset.map(
        snr_apply,
        batched=True,
        batch_size=args.batch_size,
        with_rank=True if torch.cuda.device_count()>0 else False,
        num_proc=torch.cuda.device_count()*args.num_workers_per_gpu_for_snr if torch.cuda.device_count()>0 else args.cpu_num_workers,
        remove_columns=[audio_column_name], # tricks to avoid rewritting audio
        fn_kwargs={"audio_column_name": audio_column_name},
    )
    
    print("Compute speaking rate")
    if "speech_duration" in snr_dataset[next(iter(snr_dataset.keys()))].features:    
        rate_dataset = snr_dataset.map(
            rate_apply,
            with_rank=False,
            num_proc=args.cpu_num_workers,
            writer_batch_size= args.cpu_writer_batch_size,
            fn_kwargs={"audio_column_name": audio_column_name, "text_column_name": text_column_name},
        )
    else:
        rate_dataset = dataset.map(
            rate_apply,
            with_rank=False,
            num_proc=args.cpu_num_workers,
            writer_batch_size= args.cpu_writer_batch_size,
            remove_columns=[audio_column_name], # tricks to avoid rewritting audio
            fn_kwargs={"audio_column_name": audio_column_name, "text_column_name": text_column_name},
        )
    
    for split in dataset.keys():
        dataset[split] = pitch_dataset[split].add_column("snr", snr_dataset[split]["snr"]).add_column("c50", snr_dataset[split]["c50"])
        if "speech_duration" in snr_dataset[split]:
            dataset[split] = dataset[split].add_column("speech_duration", snr_dataset[split]["speech_duration"])
        dataset[split] = dataset[split].add_column("speaking_rate", rate_dataset[split]["speaking_rate"]).add_column("phonemes", rate_dataset[split]["phonemes"])
        if args.apply_squim_quality_estimation:
            dataset[split] = dataset[split].add_column("stoi", squim_dataset[split]["stoi"]).add_column("si-sdr", squim_dataset[split]["sdr"]).add_column("pesq", squim_dataset[split]["pesq"])
    
    if args.output_dir:
        print("Saving to disk...")
        if storage_options:
            dataset.save_to_disk(args.output_dir, storage_options=storage_options)
        else:
            dataset.save_to_disk(args.output_dir)
    if args.repo_id:
        print("Pushing to the hub...")
        if args.configuration:
            dataset.push_to_hub(args.repo_id, args.configuration)
        else:
            dataset.push_to_hub(args.repo_id)

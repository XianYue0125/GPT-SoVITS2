import multiprocessing
import os
import time
import numpy as np
import onnxruntime
import torch
import torchaudio
import whisper
from tqdm import tqdm
from multiprocessing import get_context, Queue, Process
import asyncio

max_val = 0.8
option = onnxruntime.SessionOptions()
option.graph_optimization_level = onnxruntime.GraphOptimizationLevel.ORT_ENABLE_ALL
option.intra_op_num_threads = 1
dataset_dir = "./dataset"

def create_inference_session(gpu_id):
    return onnxruntime.InferenceSession(
        "./pretrained_models/speech_tokenizer_v1.onnx",
        sess_options=option,
        providers=[("CUDAExecutionProvider", {"device_id": gpu_id})],
    )

def torch_trim(speech, top_db=60, frame_length=440, hop_length=220):
    # 计算短时能量
    window = torch.hann_window(frame_length).to(speech.device)
    stft = torch.stft(
        speech[0],
        n_fft=frame_length,
        hop_length=hop_length,
        window=window,
        return_complex=True,
    )
    magnitude = torch.abs(stft)
    energy = torch.sum(magnitude**2, dim=0)

    # 计算阈值
    db_threshold = torch.max(energy) / (10 ** (top_db / 10))

    # 找到超过阈值的帧
    masks = energy > db_threshold
    valid_frames = torch.where(masks)[0]

    if len(valid_frames) > 0:
        start_frame = max(0, valid_frames[0] - 1)
        end_frame = min(len(energy) - 1, valid_frames[-1] + 1)
        start_sample = start_frame * hop_length
        end_sample = min(speech.shape[1], (end_frame + 1) * hop_length)
        return speech[:, start_sample:end_sample]
    else:
        return speech

def postprocess(speech, target_sr, top_db=60, hop_length=220, win_length=440):
    speech = torch_trim(
        speech, top_db=top_db, frame_length=win_length, hop_length=hop_length
    )
    if speech.abs().max() > max_val:
        speech = speech / speech.abs().max() * max_val
    speech = torch.concat([speech, torch.zeros(1, int(target_sr * 0.2))], dim=1)
    return speech

def load_and_preprocess_audio(file_path, target_sr):
    speech, sample_rate = torchaudio.load(file_path, backend="soundfile")
    speech = speech.mean(dim=0, keepdim=True)
    if sample_rate != target_sr:
        speech = torchaudio.transforms.Resample(orig_freq=sample_rate, new_freq=target_sr)(speech)
    speech = postprocess(speech, target_sr)
    feat = whisper.log_mel_spectrogram(speech, n_mels=128).detach().cpu().numpy()
    return feat, feat.shape[2]

def producer(data_queue, data):
    for item in tqdm(data, desc="预处理音频"):
        _, wav_name, _, index_folder = item
        wav_path = os.path.join(dataset_dir, index_folder, wav_name)
        feat, feat_length = load_and_preprocess_audio(wav_path, 16000)
        data_queue.put((feat, feat_length, item))
    data_queue.put(None)  # 发送结束信号

def process_gpu(gpu_id, data_queue, result_queue):
    session = create_inference_session(gpu_id)
    batch_size = 256
    while True:
        batch = []
        for _ in range(batch_size):
            item = data_queue.get()
            if item is None:
                if batch:
                    break
                else:
                    result_queue.put(None)
                    return
            batch.append(item)
        
        if not batch:
            break

        start_time = time.time()
        results = []
        for feat, feat_length, item in batch:
            _, wav_name, _, index_folder = item
            wav_path = os.path.join(dataset_dir, index_folder, wav_name)
            speech_token = session.run(
                None,
                {
                    session.get_inputs()[0].name: feat,
                    session.get_inputs()[1].name: np.array([feat_length], dtype=np.int32),
                },
            )[0]
            speech_token = np.array(speech_token.flatten().tolist())
            results.append((wav_path, speech_token))
        result_queue.put(results)
        print(f"process_batch时间: {time.time() - start_time} 秒，使用GPU{gpu_id}")
    result_queue.put(None)  # 发送结束信号

def consumer(result_queue, total_items):
    processed_items = 0
    pbar = tqdm(total=total_items, desc="保存结果")
    while processed_items < total_items:
        results = result_queue.get()
        if results is None:
            continue
        for wav_path, speech_token in results:
            speech_token_path = wav_path + ".npy"
            np.save(speech_token_path, speech_token)
            processed_items += 1
            pbar.update(1)
    pbar.close()
    print(f"总共处理了 {processed_items} 个项目")

async def async_process(data):
    num_gpus = torch.cuda.device_count()
    print(f"将使用 {num_gpus} 个GPU进行并行处理")

    batch_size = len(data) // num_gpus
    data_batches = [data[i*batch_size:(i+1)*batch_size] for i in range(num_gpus)]
    if len(data) % num_gpus != 0:
        data_batches[-1].extend(data[num_gpus*batch_size:])

    processes = []
    data_queues = []
    result_queue = Queue()

    for gpu_id in range(num_gpus):
        data_queue = Queue()
        data_queues.append(data_queue)

        producer_process = Process(target=producer, args=(data_queue, data_batches[gpu_id]))
        processes.append(producer_process)
        producer_process.start()

        gpu_process = Process(target=process_gpu, args=(gpu_id, data_queue, result_queue))
        processes.append(gpu_process)
        gpu_process.start()

    consumer_process = Process(target=consumer, args=(result_queue, len(data)))
    processes.append(consumer_process)
    consumer_process.start()

    for p in processes:
        p.join()

    print("所有数据处理完成")

if __name__ == "__main__":
    multiprocessing.set_start_method('spawn')
    todo = []
    for root, dirs, files in os.walk(dataset_dir):
        for file in files:
            if file.endswith(".txt"):
                index_folder = os.path.relpath(root, dataset_dir)
                file_path = os.path.join(root, file)

                # 尝试不同的编码
                encodings = ["utf-8", "gbk", "gb2312", "utf-16"]
                for encoding in encodings:
                    try:
                        with open(file_path, "r", encoding=encoding) as f:
                            lines = f.readlines()
                        break  # 如果成功读取，跳出循环
                    except UnicodeDecodeError:
                        continue  # 如果解码失败，尝试下一个编码
                else:
                    print(f"无法解码文件 {file_path}，跳过此文件")
                    continue  # 如果所有编码都失败，跳过此文件

                for line in lines:
                    try:
                        spk_name, wav_name, text = line.split("|")
                        todo.append([spk_name, wav_name, text, index_folder])
                    except Exception:
                        print(line)

    start_time = time.time()
    asyncio.run(async_process(todo))
    print(f"总处理时间: {time.time() - start_time} 秒")
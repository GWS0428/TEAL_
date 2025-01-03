import transformers
import torch
import torch.nn as nn
from transformers import AutoConfig
from collections import defaultdict

import os
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.colors import ListedColormap
import json

class SparsifyFn(nn.Module):
    def __init__(self, distr, init_sparsity=None,init_threshold=None, apply_prefill=True):
        super(SparsifyFn, self).__init__()

        assert init_sparsity is None or init_threshold is None, "init_sparsity and init_threshold cannot both be specified"

        if init_sparsity is not None:
            thresh = distr.icdf(0.5 + init_sparsity/2)
        elif init_threshold is not None:
            thresh = init_threshold
        else:
            init_sparsity = 0
            thresh = 0
        
        self.register_buffer("a", torch.tensor([thresh]).to(torch.float16))

        self.distr = distr
        self.apply_prefill = apply_prefill

    def set_threshold(self, sparsity):
        self.threshold = self.distr.icdf(0.5 + sparsity/2).item() if sparsity != 0.0 else 0.0
        self.sparsity_level = sparsity

    def set_topk(self, sparsity):
        self.sparsity_level = sparsity
        self.sparsity_type = "topk"

    def set_topp(self, sparsity):
        self.sparsity_level = sparsity
        self.sparsity_type = "topp"

    def forward(self, x):

        # NOTE: we can + should change this to sparsify 99% of tokens instead of 50%
        # I just finished the evals for the paper at 50% before I noticed the prefill sparsification phenomenon (Section 5.4.3)
        if x.size(1) > 1 and self.apply_prefill:
            half_seq_len = x.size(1) // 2
            # half_seq_len = int(0.99 * x.size(1))
            last_context = x[:, -half_seq_len:, :]
            modified_context = self.apply(last_context)
            
            x = torch.cat((x[:, :-half_seq_len, :], modified_context), dim=1)
            return x
        
        if x.size(1) > 1 and not self.apply_prefill:
            return x

        assert x.size(1) == 1, "supposedly x is decode only"
        return self.apply(x)
    
    def forward_runtime_pruning_topk(self, x):
        if x.size(1) > 1:
            half_seq_len = x.size(1) // 2
            last_context = x[:, -half_seq_len:, :]
            modified_context = self.apply_runtime_pruning_topk(last_context, self.sparsity_level)
            
            x = torch.cat((x[:, :-half_seq_len, :], modified_context), dim=1)
            return x
        
        if x.size(1) > 1 and not self.apply_prefill:
            return x
        
        assert x.size(1) == 1, "supposedly x is decode only"
        return self.apply_runtime_pruning_topk(x, self.sparsity_level)
    
    def apply(self, x):
        return x.abs().gt(self.threshold) * x

    def apply_runtime_pruning_topk(self, x, sparsity):
        if sparsity == 0.0:
            return torch.zeros_like(x)
        elif sparsity == 1.0:
            return x

        # Compute the number of elements to keep
        num_elements = x.numel()
        k = int(num_elements * sparsity)
        if k == 0:
            return torch.zeros_like(x)

        # Compute top-k absolute values
        threshold, _ = torch.kthvalue(x.abs().view(-1), num_elements - k + 1)
        
        # Create a mask for top-k values
        mask = x.abs() >= threshold
        return x * mask
    
    def get_threshold(self):
        return self.threshold


def interp(x, xp, fp):
    """Custom interpolation function for PyTorch tensors."""
    i = torch.searchsorted(xp, x)
    i = torch.clamp(i, 1, len(xp) - 1)
    
    xp_left = xp[i - 1]
    xp_right = xp[i]
    fp_left = fp[i - 1]
    fp_right = fp[i]
    
    t = (x - xp_left) / (xp_right - xp_left)
    return fp_left + t * (fp_right - fp_left)


class Distribution:
    def __init__(self, file_path, hidden_type):
        self.file_path = file_path
        self.hidden_type = hidden_type # h1 or h2
        
        histogram = torch.load(f"{self.file_path}/histograms.pt")

        self.bin_centers, self.counts = histogram[f"{self.hidden_type}_centers"], histogram[self.hidden_type]

        self.total_count = self.counts.sum()
        self.cumulative_counts = torch.cumsum(self.counts, dim=0)

    # kernel smoothing
    def pdf(self, x, bandwidth=None):
        if bandwidth is None:
            bandwidth =  1.06 * torch.std(self.bin_centers[1:-1]) * (self.total_count-2)**(-1/5)
        
        bin_centers = self.bin_centers.unsqueeze(1)
        
        if isinstance(x, float) or isinstance(x, int):
            x = torch.tensor([x])
        else:
            x = x.unsqueeze(0)
        
        kernel = torch.exp(-0.5 * ((x - bin_centers) / bandwidth)**2) / (bandwidth * torch.sqrt(torch.tensor(2 * torch.pi)))
        pdf = torch.sum(kernel * self.counts.unsqueeze(1), dim=0) / self.total_count
        
        return pdf
    
    def cdf(self, x):
        return interp(x, self.bin_centers, self.cumulative_counts / self.total_count)
    
    # NOTE: Assumes distribution is zero mean unimodal
    def icdf(self, q):
        # if q < 0.01 or q > 0.99:
        #     print(f"WARNING: All outliers clip to the most extreme bin")

        target_count = q * self.total_count
        idx = torch.searchsorted(self.cumulative_counts, target_count)
        
        if idx == 0:
            return self.bin_centers[0]
        elif idx == len(self.bin_centers):
            return self.bin_centers[-1]
        else:
            lower_count = self.cumulative_counts[idx - 1]
            upper_count = self.cumulative_counts[idx]
            lower_value = self.bin_centers[idx - 1]
            upper_value = self.bin_centers[idx]
            
            fraction = (target_count - lower_count) / (upper_count - lower_count)
            return lower_value + fraction * (upper_value - lower_value)

class ActivationModule:
    def __init__(self, file_path):
        self.file_path = file_path
        self.activations = defaultdict(list)
        self.histograms = None
        
        # store is to store stuff like position_ids in attn (for convinience, is bad code)
        self.store = {}

    def grab_activations(self, x, key):
        if x.size(1) > 1:  # Check if seq_len > 1
            self.activations[key].append(x.detach().squeeze(0).cpu().float())
    def save_activations(self):
        self.activations = self.combine_activations()
        torch.save(self.activations, f"{self.file_path}/activations.pt")

    def load_activations(self):
        self.activations = torch.load(f"{self.file_path}/activations.pt")

    # NOTE: This doesn't store outlier activation values
    def find_histogram(self, num_bins=10000, outlier_threshold=0.01):
        if self.histograms is None:
            # for fine-grained analysis, do not combine activations
            self.activations = self.combine_activations()
            self.histograms = {}
        else:
            return self.histograms
        
        torch.cuda.empty_cache()
        for key, acts in self.activations.items():

            acts = acts.flatten().detach().to('cuda')
            acts = torch.sort(acts)[0]

            lower_bound = acts[int(outlier_threshold * len(acts))]
            upper_bound = acts[-int(outlier_threshold * len(acts))]

            acts = acts.cpu()

            main_bins = torch.linspace(lower_bound, upper_bound, num_bins - 1)
            bins = torch.cat([torch.tensor([acts[0]]), main_bins, torch.tensor([acts[-1]])])

            counts, _ = torch.histogram(acts, bins=bins)

            bin_centers = (bins[:-1] + bins[1:]) / 2

            self.histograms[key] = counts.float().cpu()
            self.histograms[f"{key}_centers"] = bin_centers.float().cpu()
        return self.histograms
    
    def save_histogram(self):
        os.makedirs(self.file_path, exist_ok=True)
        torch.save(self.histograms, f"{self.file_path}/histograms.pt")

    def combine_activations(self):
        combined_activations = {}
        for key, acts in self.activations.items():
            combined_activations[key] = torch.cat(acts, dim=0)
        return combined_activations

from transformers import AutoConfig

def get_model_class_name(model_name):
    try:
        # Fetch the model config
        config = AutoConfig.from_pretrained(model_name)
        
        # Get the model class name from the config
        model_class_name = config.architectures[0] if config.architectures else None
        
        return model_class_name
    except Exception as e:
        print(f"Error fetching model class name: {e}")
        return None


def get_sparse_model(model_name, device, histogram_path, **kwargs):
    from teal.model import LlamaSparseForCausalLM, MistralSparseForCausalLM, LlamaSparseConfig, MistralSparseConfig

    from transformers import AutoConfig, AutoModelForCausalLM

    AutoConfig.register("llama_sparse", LlamaSparseConfig)
    AutoModelForCausalLM.register(LlamaSparseConfig, LlamaSparseForCausalLM)
    AutoConfig.register("mistral_sparse", MistralSparseConfig)
    AutoModelForCausalLM.register(MistralSparseConfig, MistralSparseForCausalLM)

    class_name = get_model_class_name(model_name)

    assert class_name in ["LlamaForCausalLM", "MistralForCausalLM", "LlamaSparseForCausalLM", "MistralSparseForCausalLM"], f"Model class name {class_name} not supported"

    SparseModel = LlamaSparseForCausalLM if "Llama" in class_name else MistralSparseForCausalLM

    if device == 'auto':
        # multi gpu
        return SparseModel.from_pretrained(model_name, torch_dtype=torch.float16, device_map="auto", attn_implementation="flash_attention_2", histogram_path=histogram_path, **kwargs)
    else:
        return SparseModel.from_pretrained(model_name, torch_dtype=torch.float16, device_map=device, attn_implementation="flash_attention_2", histogram_path=histogram_path, **kwargs)

def get_tokenizer(tokenizer_name):
    tokenizer = transformers.AutoTokenizer.from_pretrained(
        tokenizer_name, use_fast=True, trust_remote_code=True
    )

    if tokenizer.pad_token_id is None:
        if tokenizer.eos_token_id is not None:
            tokenizer.pad_token_id = tokenizer.eos_token_id
        else:
            tokenizer.pad_token_id = 0

    return tokenizer


def get_module_device(module):
    return next(module.parameters()).device




def get_layer_greedy_sparsities(layer_sparsities, results_dir):
    import pandas as pd
    num_layers = len(layer_sparsities)
    projs = ['q', 'k', 'v', 'o', 'gate', 'up', 'down']
    sparsities = {proj: [0.0] * num_layers for proj in projs}
    
    for layer, target_sparsity in enumerate(layer_sparsities):
        file_path = os.path.join(results_dir, f'layer-{layer}', 'results.csv')
        df = pd.read_csv(file_path)
        
        # Find the row with the closest effective sparsity
        closest_row = df.iloc[(df['Effective Sparsity'] - target_sparsity).abs().argsort()[:1]]
        
        for proj in projs:
            sparsities[proj][layer] = closest_row[proj].values[0]
    
    return sparsities


def visualize_zeros(tensor, layer_idx, head_idx, output_path="zero_values_matrix.png"):
    """
    Visualize the zero values in an (n, m) matrix extracted from a (1, 1, n, m) tensor.

    Parameters:
        tensor (numpy.ndarray): A tensor of shape (1, 1, n, m).
        layer_idx (int): The index of the layer.
        head_idx (int): The index of the attention head.
        output_path (str): Path to save the generated figure.
    """
    # Extract the (n, m) matrix
    matrix = tensor[0, head_idx, :, :]
    matrix = matrix.cpu().numpy()

    # Create a binary mask: 0 for zeros, 1 for non-zeros
    zero_mask = (matrix == 0).astype(int)

    # Define a colormap: red for zeros, black for non-zeros
    cmap = ListedColormap(["red", "black"])

    # Create a plot
    plt.figure(figsize=(8, 6))
    plt.imshow(zero_mask, cmap=cmap, interpolation="none")
    plt.colorbar(label="Zero Highlight (Red)")
    plt.title(f"Zero Values in the Matrix for Layer {layer_idx}, Head {head_idx}")
    plt.xlabel("Columns")
    plt.ylabel("Rows")

    # Save the figure to the specified path
    plt.savefig(output_path)
    plt.close()


def analyze_kv_cache_zeros(model):
    """
    Analyzes zero indices in the kv_cache.k_cache tensors for each layer in the model.
    
    Args:
        model: The model containing layers with kv_cache.k_cache tensors.

    Prints:
        - Zero indices for each (head, sequence) pair in each layer.
        - Statistics (average and standard deviation of zero counts) for each layer.
    """
    current_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(current_dir)
    results_dir = os.path.join(parent_dir, "profile")
    os.makedirs(results_dir, exist_ok=True)

    for layer_idx, layer in enumerate(model.layers):
        if layer_idx != 10:
            continue
        # Check if the layer has attention.kv_cache.k_cache
        if hasattr(layer, 'attention') and hasattr(layer.attention, 'kv_cache'):
            k_cache = layer.attention.kv_cache.k_cache
            batch_size, num_heads, seq_length, head_dim = k_cache.shape
            
            zero_counts = []  # Store the count of zeros for statistics
            
            for head in range(num_heads):
                if head != 10:
                    continue
                for seq in range(seq_length):
                    # Extract the (1, 1, 1, 128) vector
                    vector = k_cache[0, head, seq, :]

                    # only look at the token containing nonzeros
                    if torch.all(vector != 0):
                        continue
                    
                    # Find indices of zeros
                    zeros = torch.nonzero(vector == 0, as_tuple=True)[0]
                    zero_counts.append(len(zeros))  # Count of zeros
                    
                os.makedirs(f"{results_dir}/layer-{layer_idx}", exist_ok=True)  
                visualize_zeros(k_cache, layer_idx, head, f"{results_dir}/layer-{layer_idx}/head-{head}.png")
            
            # Calculate statistics
            avg_zeros = np.mean(zero_counts)
            std_zeros = np.std(zero_counts)

            # store stats in a file
            os.makedirs(f"{results_dir}/layer-{layer_idx}", exist_ok=True)  
            with open(f"{results_dir}/layer-{layer_idx}/stats.json", "w") as f:
                json.dump({"avg_zeros": avg_zeros, "std_zeros": std_zeros}, f)


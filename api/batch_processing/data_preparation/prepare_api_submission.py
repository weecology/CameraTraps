"""
This module is somewhere between "documentation" and "code".  It is intended to
capture the steps the precede running a task via the AI for Earth Camera Trap
Image Processing API, and it automates a couple of those steps.  We hope to
gradually automate all of these.

Here's the stuff we usually do before submitting a task:

1) Upload data to Azure... we do this with azcopy, not addressed in this script

2) List the files you want the API to process... see
    ai4eutils.ai4e_azure_utils.enumerate_blobs_to_file()

3) Divide that list into chunks that will become individual API submissions...
    this module supports that via divide_files_into_tasks.

3) Put each .json file in a blob container, and generate a read-only SAS
   URL for it.  Not automated right now.

4) Generate the API query(ies) you'll submit to the API... see
    generate_api_queries()

5) Submit the API query... I currently do this with Postman.

6) Monitor task status

7) Combine multiple API outputs

8) We're now into what we really call "postprocessing", rather than
    "data_preparation", but... possibly do some amount of partner-specific
    renaming, folder manipulation, etc. This is very partner-specific, but
    generally done via:

    find_repeat_detections.py
    subset_json_detector_output.py
    postprocess_batch_results.py
"""
#%% Imports and constants

from __future__ import annotations

from enum import Enum
import json
import os
import posixpath
import string
from typing import Any, Dict, List, Optional, Sequence, Tuple
import urllib

import requests

import ai4e_azure_utils  # from ai4eutils
import path_utils  # from ai4eutils


MAX_FILES_PER_API_TASK = 1_000_000
IMAGES_PER_SHARD = 2000

VALID_REQUEST_NAME_CHARS = f'-_{string.ascii_letters}{string.digits}'
REQUEST_NAME_CHAR_LIMIT = 92


class BatchAPISubmissionError(Exception):
    pass


class TaskStatus(str, Enum):
    RUNNING = 'running'
    FAILED = 'failed'
    PROBLEM = 'problem'
    COMPLETED = 'completed'


class Task:
    """Represents a Batch Processing API task."""

    # instance variables
    name: str
    local_images_list_path: str
    remote_images_list_url: str
    api_request: Dict[str, Any]
    id: str
    response: Dict[str, Any]

    def __init__(self, name: str, images_list_path: str, local: bool,
                 validate: bool = True):
        """Initializes a Task.

        If desired, validates that the images list does not exceed the maximum
        length and that all files in the images list are actually images.

        Args:
            name: str, name of the request
            images_list_path: str, path or URL to a JSON file containing a list
                of image paths
            local: bool, set to True if images_list_path is a local path,
                set to False if images_list_path is a URL
            validate: bool, whether to validate the given images list
        """
        clean_name = clean_request_name(name)
        if name != clean_name:
            print(f'Warning: renamed {name} to {clean_name}')
        self.name = clean_name

        if local:
            self.local_images_list_path = images_list_path
        else:
            self.remote_images_list_url = images_list_path

        if validate:
            if local:
                with open(images_list_path, 'r') as f:
                    images_list = json.load(f)
            else:
                images_list = requests.get(images_list_path).json()

            if len(images_list) > MAX_FILES_PER_API_TASK:
                raise ValueError('images list has too many files')

            for img_path in images_list:
                if not path_utils.is_image_file(img_path):
                    raise ValueError(f'{img_path} is not an image')

    @classmethod
    def from_task_id(cls, task_id: str, task_status_endpoint_url: str,
                     name: Optional[str] = None) -> Task:
        """Alternative constructor for a Task object from an existing task ID.

        Args:
            task_id: str, ID of a submitted task
            task_status_endpoint_url: str
            name: optional str, task name, defaults to task_id

        Returns: dict, contains fields ['Status', 'TaskId'] and possibly others

        Raises: requests.HTTPError, if an HTTP error occurred
        """
        url = posixpath.join(task_status_endpoint_url, task_id)
        r = requests.get(url)

        r.raise_for_status()
        assert r.status_code == requests.codes.ok

        response = r.json()
        task = cls(name=name if name is not None else task_id,
                   images_list_path=response['Status']['message']['images'],
                   local=False)
        task.id = task_id
        task.response = response
        return task

    def upload_images_list(self, account: str, container: str, sas_token: str,
                           blob_name: Optional[str] = None) -> None:
        """Uploads the local images list to an Azure Blob Storage container.

        Args:
            account: str, Azure Storage account name
            container: str, Azure Blob Storage container name
            sas_token: str, Shared Access Signature (SAS) with write permission,
                does not start with '?'
            blob_name: optional str, defaults to basename of
                self.local_images_list_path if blob_name is not given
        """
        if blob_name is None:
            blob_name = os.path.basename(self.local_images_list_path)
        blob_url = ai4e_azure_utils.upload_file_to_blob(
            account_name=account, container_name=container,
            local_path=self.local_images_list_path, blob_name=blob_name,
            sas_token=sas_token)
        self.remote_images_list_url = f'{blob_url}?{sas_token}'

    def generate_api_request(self,
                             caller: str,
                             input_container_url: Optional[str] = None,
                             image_path_prefix: Optional[str] = None,
                             **kwargs: Any
                             ) -> Dict[str, Any]:
        """Generate API request JSON.

        For complete list of API input parameters, see:
        https://github.com/microsoft/CameraTraps/tree/master/api/batch_processing#api-inputs

        Args:
            caller: str
            input_container_url: optional str, URL to Azure Blob Storage
                container where images are stored. URL must include SAS token
                with read and list permissions if the container is not public.
                Only provide this parameter when the image paths in
                self.remote_images_list_url are relative to a container.
            image_path_prefix: optional str, TODO
            kwargs: additional API input parameters

        Returns: dict, represents the JSON request to be submitted
        """
        request = kwargs
        request.update({
            'request_name': self.name,
            'caller': caller,
            'images_requested_json_sas': self.remote_images_list_url
        })
        if input_container_url is None:
            request['use_url'] = True  # TODO: check how `use_url` is used
        else:
            request['input_container_sas'] = input_container_url
        if image_path_prefix is not None:
            request['image_path_prefix'] = image_path_prefix
        self.api_request = request
        return request

    def submit(self, request_endpoint_url: str) -> str:
        """Submit this task to the Batch Processing API.

        Only run this method after generate_api_request().

        Args:
            request_endpoint_url: str, URL of request endpoint

        Returns: str, task ID

        Raises:
            requests.HTTPError, if an HTTP error occurred
            BatchAPISubmissionError, if request returns an error
        """
        r = requests.post(request_endpoint_url, json=self.api_request)
        r.raise_for_status()
        assert r.status_code == requests.codes.ok

        response = r.json()
        if 'error' in response:
            raise BatchAPISubmissionError(response['error'])
        if 'request_id' not in response:
            raise BatchAPISubmissionError(
                f'"request_id" not in API response: {response}')
        self.id = response['request_id']
        return self.id

    def check_status(self, task_status_endpoint_url: str) -> Dict[str, Any]:
        """Fetch the .json content from the task URL

        Args:
            task_status_endpoint_url: str

        Returns: dict, contains fields ['Status', 'TaskId'] and possibly others

        Raises: requests.HTTPError, if an HTTP error occurred
        """
        url = posixpath.join(task_status_endpoint_url, self.id)
        r = requests.get(url)

        r.raise_for_status()
        assert r.status_code == requests.codes.ok

        self.response = r.json()
        return self.response

    def get_missing_images(self, verbose: bool = False) -> List[str]:
        """Compares the submitted and processed images lists to find missing
        images. Double-checks that 'failed_images' is a subset of the missing
        images.

        "missing": an image from the submitted list that was not processed,
            for whatever reason
        "failed": a missing image explicitly marked as 'failed' by the
            batch processing API

        Only run this method after check_status() returns a response where
        response['Status']['request_status'] == TaskStatus.COMPLETED.

        Returns: list of str, sorted list of missing image paths
        """
        assert self.response['Status']['request_status'] == TaskStatus.COMPLETED
        message = self.response['Status']['message']

        # estimate # of failed images from failed shards
        n_failed_shards = message['num_failed_shards']
        estimated_failed_shard_images = n_failed_shards * IMAGES_PER_SHARD

        # Download all three JSON urls to memory
        output_file_urls = message['output_file_urls']
        submitted_images = requests.get(output_file_urls['images']).json()
        detections = requests.get(output_file_urls['detections']).json()
        failed_images = requests.get(output_file_urls['failed_images']).json()

        assert all(path_utils.find_image_strings(s) for s in submitted_images)
        assert all(path_utils.find_image_strings(s) for s in failed_images)

        # Diff submitted and processed images
        processed_images = [d['file'] for d in detections['images']]
        missing_images = sorted(set(submitted_images) - set(processed_images))

        if verbose:
            print(f'Submitted {len(submitted_images)} images')
            print(f'Received results for {len(processed_images)} images')
            print(f'{len(failed_images)} failed images')
            print(f'{n_failed_shards} failed shards '
                  f'(~approx. {estimated_failed_shard_images} images)')
            print(f'{len(missing_images)} images not in results')

        # Confirm that the failed images are a subset of the missing images
        assert set(failed_images) <= set(missing_images), (
            'Failed images should be a subset of missing images')

        return missing_images


#%% Dividing files into multiple tasks

def divide_chunks(l: Sequence[Any], n: int) -> List[Sequence[Any]]:
    """
    Divide list *l* into chunks of size *n*, with the last chunk containing
    <= n items.
    """
    # https://www.geeksforgeeks.org/break-list-chunks-size-n-python/
    chunks = [l[i * n:(i + 1) * n] for i in range((len(l) + n - 1) // n)]
    return chunks


def divide_files_into_tasks(
        file_list_json: str,
        n_files_per_task: int = MAX_FILES_PER_API_TASK
        ) -> Tuple[List[str], List[Sequence[Any]]]:
    """
    Divides the file *file_list_json*, which contains a single json-encoded list
    of strings, into a set of json files, each containing *n_files_per_task*
    (the last file will contain <= *n_files_per_task* files).

    Output JSON files have extension `*.chunkXXX.json`. For example, if the
    input JSON file is `blah.json`, output files will be `blah.chunk000.json`,
    `blah.chunk001.json`, etc.

    Args:
        file_list_json: str, path to JSON file containing list of file names
        n_files_per_task: int, max number of files to include in each API task

    Returns:
        output_files: list of str, output JSON file names
        chunks: list of list of str, chunks[i] is the content of output_files[i]
    """
    with open(file_list_json) as f:
        file_list = json.load(f)

    chunks = divide_chunks(file_list, n_files_per_task)
    output_files = []

    for i_chunk, chunk in enumerate(chunks):
        chunk_id = f'chunk{i_chunk:0>3d}'
        output_file = path_utils.insert_before_extension(
            file_list_json, chunk_id)
        output_files.append(output_file)
        with open(output_file, 'w') as f:
            json.dump(chunk, f, indent=1)

    return output_files, chunks


def clean_request_name(request_name: str,
                       whitelist: str = VALID_REQUEST_NAME_CHARS,
                       char_limit: int = REQUEST_NAME_CHAR_LIMIT) -> str:
    """Removes invalid characters from an API request name."""
    return path_utils.clean_filename(
        filename=request_name, whitelist=whitelist, char_limit=char_limit)


def download_url(url: str, save_path: str, verbose: bool = False) -> None:
    """Download a URL to a local file."""
    if verbose:
        print(f'Downloading {url} to {save_path}')
    urllib.request.urlretrieve(url, save_path)
    assert os.path.isfile(save_path)


#%% Interactive driver

# if False:

#     #%%
#     account_name = ''
#     sas_token = 'st=...'
#     container_name = ''
#     rmatch = None # '^Y53'
#     output_file = r'output.json'

#     blobs = ai4e_azure_utils.enumerate_blobs_to_file(
#         output_file=output_file,
#         account_name=account_name,
#         sas_token=sas_token,
#         container_name=container_name,
#         rsearch=rsearch)

#     #%%

#     file_list_json = r"D:\temp\idfg_20190801-hddrop_image_list.json"
#     task_files = divide_files_into_tasks(file_list_json)

#     #%%

#     file_list_sas_urls = [
#         '','',''
#     ]

#     input_container_sas_url = ''
#     request_name_base = ''
#     caller = 'blah@blah.com'

#     request_strings,request_dicts = generate_api_queries(
#         input_container_sas_url,
#         file_list_sas_urls,
#         request_name_base,
#         caller)

#     for s in request_strings:
#         print(s)

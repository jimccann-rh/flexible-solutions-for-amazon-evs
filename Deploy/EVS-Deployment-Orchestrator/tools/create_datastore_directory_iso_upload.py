#!/usr/bin/env python3
"""
Create a directory on a vSAN datastore via vCenter and optionally upload files.

Retrieves the vCenter administrator password from AWS Secrets Manager,
derives the vCenter FQDN from config.json, connects to vCenter, and
creates the specified directory on the vSAN datastore. Can also upload
ISO files (or any file) to that directory.

Requirements:
  pip install boto3 pyvmomi

Usage:
  # Create the isos directory:
  python create_datastore_directory_iso_upload.py --profile ci \\
      --config ../../Phase_2_evs_env/python/config.json

  # Create directory and upload an ISO:
  python create_datastore_directory_iso_upload.py --profile ci \\
      --config ../../Phase_2_evs_env/python/config.json \\
      --upload /path/to/rhcos-live.iso

  # Upload multiple files:
  python create_datastore_directory_iso_upload.py --profile ci \\
      --config ../../Phase_2_evs_env/python/config.json \\
      --upload /path/to/rhcos-live.iso /path/to/other.iso

  # Override datastore name and directory:
  python create_datastore_directory_iso_upload.py --profile ci \\
      --config ../../Phase_2_evs_env/python/config.json \\
      --datastore my-datastore --directory my-isos \\
      --upload /path/to/rhcos-live.iso
"""

import argparse
import http.client
import json
import logging
import os
import ssl
import sys
import time
import urllib.parse

import boto3
from pyVim import connect
from pyVmomi import vim

LOG = logging.getLogger("create_datastore_directory")

CHUNK_SIZE = 16 * 1024 * 1024  # 16 MB upload chunks


# ---------------------------------------------------------------------------
# AWS helpers
# ---------------------------------------------------------------------------

def get_secret_password(sm, secret_id):
    """Retrieve a password from Secrets Manager.

    Handles both JSON ``{"password": "..."}`` and raw-string formats.
    """
    LOG.info("Fetching secret %s", secret_id)
    response = sm.get_secret_value(SecretId=secret_id)
    raw = response.get("SecretString", "")
    try:
        data = json.loads(raw)
        return data.get("password", raw)
    except (json.JSONDecodeError, TypeError):
        return raw


# ---------------------------------------------------------------------------
# vCenter helpers
# ---------------------------------------------------------------------------

def connect_to_vcenter(host, username, password, port=443):
    ctx = ssl._create_unverified_context()
    return connect.SmartConnect(
        host=host, user=username, pwd=password,
        port=port, sslContext=ctx,
    )


def find_cluster(si, cluster_name):
    content = si.RetrieveContent()
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.ClusterComputeResource], True,
    )
    try:
        for cluster in container.view:
            if cluster.name == cluster_name:
                return cluster
    finally:
        container.Destroy()

    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.ClusterComputeResource], True,
    )
    try:
        available = [c.name for c in container.view]
    finally:
        container.Destroy()
    raise RuntimeError(
        f"Cluster '{cluster_name}' not found. Available: {available}"
    )


def find_vsan_datastore(cluster):
    """Find the vSAN datastore on a cluster."""
    for host in cluster.host:
        for ds in host.datastore:
            if getattr(ds.summary, "type", "").lower() == "vsan":
                return ds

    available = set()
    for host in cluster.host:
        for ds in host.datastore:
            available.add(f"{ds.name} ({ds.summary.type})")
    raise RuntimeError(
        f"No vSAN datastore found on cluster '{cluster.name}'. "
        f"Available: {sorted(available)}"
    )


def find_datastore_by_name(cluster, datastore_name):
    """Find a datastore by name on a cluster."""
    for host in cluster.host:
        for ds in host.datastore:
            if ds.name == datastore_name:
                return ds

    available = set()
    for host in cluster.host:
        for ds in host.datastore:
            available.add(ds.name)
    raise RuntimeError(
        f"Datastore '{datastore_name}' not found on cluster "
        f"'{cluster.name}'. Available: {sorted(available)}"
    )


def find_datacenter_for_datastore(datastore):
    """Walk up the inventory tree from a datastore to find its datacenter."""
    parent = datastore.parent
    while parent:
        if isinstance(parent, vim.Datacenter):
            return parent
        parent = getattr(parent, "parent", None)
    raise RuntimeError(
        f"Could not find datacenter for datastore '{datastore.name}'"
    )


def create_directory_on_datastore(si, datastore, directory_name):
    """Create a directory on the datastore via the FileManager API."""
    content = si.RetrieveContent()
    dc = find_datacenter_for_datastore(datastore)
    ds_path = f"[{datastore.name}] {directory_name}"

    LOG.info("Creating directory: %s", ds_path)
    try:
        content.fileManager.MakeDirectory(
            name=ds_path,
            datacenter=dc,
            createParentDirectories=True,
        )
        print(f"Directory created: {ds_path}")
    except vim.fault.FileAlreadyExists:
        print(f"Directory already exists: {ds_path}")


# ---------------------------------------------------------------------------
# File upload
# ---------------------------------------------------------------------------

def upload_file_to_datastore(si, vcenter_host, datastore, directory_name,
                             local_path):
    """Upload a local file to a datastore directory via vCenter HTTP access."""
    filename = os.path.basename(local_path)
    file_size = os.path.getsize(local_path)

    dc = find_datacenter_for_datastore(datastore)

    remote_path = urllib.parse.quote(f"{directory_name}/{filename}")
    dc_name = urllib.parse.quote(dc.name)
    ds_name = urllib.parse.quote(datastore.name)
    url_path = f"/folder/{remote_path}?dcPath={dc_name}&dsName={ds_name}"

    # Reuse the pyVmomi SOAP session cookie for authentication
    raw_cookie = si._stub.cookie
    cookie_name = raw_cookie.split("=", 1)[0]
    cookie_value = raw_cookie.split("=", 1)[1].split(";")[0]
    cookie_header = f"{cookie_name}={cookie_value}"

    ctx = ssl._create_unverified_context()
    conn = http.client.HTTPSConnection(vcenter_host, 443, context=ctx)

    headers = {
        "Content-Type": "application/octet-stream",
        "Content-Length": str(file_size),
        "Cookie": cookie_header,
    }

    LOG.info("Uploading %s (%.2f GB) -> [%s] %s/%s",
             filename, file_size / (1024 ** 3), datastore.name,
             directory_name, filename)

    conn.putrequest("PUT", url_path)
    for k, v in headers.items():
        conn.putheader(k, v)
    conn.endheaders()

    with open(local_path, "rb") as f:
        uploaded = 0
        last_pct_logged = -10
        start_time = time.time()

        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            conn.send(chunk)
            uploaded += len(chunk)
            pct = uploaded * 100 // file_size if file_size else 100
            if pct >= last_pct_logged + 10:
                LOG.info("  %s: %d%% uploaded", filename, pct)
                last_pct_logged = pct

    resp = conn.getresponse()
    elapsed = time.time() - start_time
    conn.close()

    if resp.status in (200, 201):
        speed_mb = (file_size / (1024 ** 2)) / elapsed if elapsed > 0 else 0
        LOG.info("Upload complete: %s (%.2f GB in %.0fs, %.1f MB/s)",
                 filename, file_size / (1024 ** 3), elapsed, speed_mb)
        print(f"Uploaded: [{datastore.name}] {directory_name}/{filename}")
    else:
        body = resp.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Upload failed for {filename}: HTTP {resp.status} {resp.reason}"
            f"\n{body}"
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Create a directory on a vSAN datastore via vCenter",
    )
    parser.add_argument(
        "--config", required=True, help="Path to config.json",
    )
    parser.add_argument("--profile", help="AWS CLI profile name")
    parser.add_argument("--region", help="AWS region override")
    parser.add_argument(
        "--datastore",
        help="Datastore name override (default: auto-detect vSAN datastore "
             "on the cluster)",
    )
    parser.add_argument(
        "--directory", default="isos",
        help="Directory name to create on the datastore (default: 'isos')",
    )
    parser.add_argument(
        "--vcenter-host",
        help="vCenter hostname/IP override "
             "(default: derived from config.json vcfHostnames.vcenter + fqdn)",
    )
    parser.add_argument(
        "--vcenter-user", default="administrator@vsphere.local",
        help="vCenter username (default: 'administrator@vsphere.local')",
    )
    parser.add_argument(
        "--cluster",
        help="Cluster name override (default: '{environmentId}-cl01')",
    )
    parser.add_argument(
        "--upload", nargs="+", metavar="FILE",
        help="Upload one or more files (e.g. ISOs) to the datastore directory",
    )
    parser.add_argument(
        "--showpassword", action="store_true",
        help="Display vCenter credentials on screen",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )

    # Validate upload files exist before doing any AWS/vCenter work
    if args.upload:
        for path in args.upload:
            if not os.path.isfile(path):
                LOG.error("File not found: %s", path)
                sys.exit(1)

    with open(args.config) as f:
        config = json.load(f)

    region = args.region or config.get("region", "us-east-1")
    environment_id = config["environmentId"]
    fqdn = config["fqdn"]

    # --- 1. Derive vCenter hostname ---
    if args.vcenter_host:
        vcenter_host = args.vcenter_host
    else:
        vc_short = config.get("vcfHostnames", {}).get("vcenter", "vc")
        vcenter_host = f"{vc_short}.{fqdn}"
    LOG.info("vCenter: %s", vcenter_host)

    # --- 2. Derive cluster name ---
    cluster_name = args.cluster or f"{environment_id}-cl01"
    LOG.info("Cluster: %s", cluster_name)

    # --- 3. Get vCenter password from Secrets Manager ---
    session_kwargs = {"region_name": region}
    if args.profile:
        session_kwargs["profile_name"] = args.profile
    session = boto3.Session(**session_kwargs)
    sm = session.client("secretsmanager")

    secret_id = f"evs-{environment_id}_vcenterSso"
    vcenter_password = get_secret_password(sm, secret_id)
    LOG.info("vCenter password retrieved")

    if args.showpassword:
        print(f"\n  vCenter Host : {vcenter_host}")
        print(f"  vCenter User : {args.vcenter_user}")
        print(f"  vCenter Pass : {vcenter_password}\n")

    # --- 4. Connect to vCenter ---
    LOG.info("Connecting to vCenter %s...", vcenter_host)
    si = connect_to_vcenter(vcenter_host, args.vcenter_user, vcenter_password)
    LOG.info("Connected")

    try:
        # --- 5. Find cluster and datastore ---
        cluster = find_cluster(si, cluster_name)

        if args.datastore:
            LOG.info("Looking for datastore '%s' on cluster '%s'...",
                     args.datastore, cluster_name)
            datastore = find_datastore_by_name(cluster, args.datastore)
        else:
            LOG.info("Looking for vSAN datastore on cluster '%s'...",
                     cluster_name)
            datastore = find_vsan_datastore(cluster)

        capacity_gb = datastore.summary.capacity / (1024 ** 3)
        free_gb = datastore.summary.freeSpace / (1024 ** 3)
        LOG.info("Datastore: %s (%.1f GB total, %.1f GB free)",
                 datastore.name, capacity_gb, free_gb)

        # --- 6. Create directory ---
        create_directory_on_datastore(si, datastore, args.directory)

        # --- 7. Upload files ---
        if args.upload:
            for path in args.upload:
                upload_file_to_datastore(
                    si, vcenter_host, datastore, args.directory, path,
                )
    finally:
        connect.Disconnect(si)


if __name__ == "__main__":
    main()

#!/usr/bin/env python2.7

#We use v2 of the google drive API:
#https://developers.google.com/resources/api-libraries/documentation/drive/v2/python/latest/drive_v2.files.html

import apiclient.errors
import apiclient.http
from argparse import ArgumentDefaultsHelpFormatter
from argparse import ArgumentParser
from argparse import FileType
import backoff
import datetime
import gc
import httplib
import httplib2
import json
import logging
import mimetypes
import os
from pydrive.auth import GoogleAuth, RefreshError
from pydrive.drive import GoogleDrive
import random
import re
import signal
import socket
import ssl
import subprocess
import sys
import time
import warnings


# Set these variables if you want to distribute gdcp with preset
# Google API OAuth Client ID details. Otherwise, if these variables are
# set to a value that evaluates to False, you will be required to provide
# OAuth Client ID details from your own Google developer console.
CLIENT_ID = None
CLIENT_SECRET = None

VERSION = "0.8.1"
PROJ = "gdcp"  # name of this project
CHUNKSIZE = 2 ** 20 * 64  # 64 MiB chunks


log = logging.getLogger(PROJ)
log.addHandler(logging.NullHandler())
googleapi = logging.getLogger("googleapiclient.discovery")
oauth2_client = logging.getLogger("oauth2client.client")
oauth2_util = logging.getLogger("oauth2client.util")
backoff_log = logging.getLogger('backoff')

class Gdcp(object):
    def __init__(self, drive, excludes=None, include=False, exclude_folders=False):
        self.drive = drive
        if not excludes:
            excludes = []
        self.excludes = [re.compile(e) for e in excludes]
        self.include = include
        # Should folders be considered in exclude rules?
        # By default rules only apply to files
        self.exclude_folders = exclude_folders

        self.file_count = 0
        self.failures = {"HTTP": [], "MD5": []}

        # https://developers.google.com/resources/api-libraries/documentation/drive/v2/python/latest/drive_v2.about.html
        #self.about = drive.auth.service.about().get().execute()

    def failed(self):
        return bool(len(self.failures["HTTP"]) or len(self.failures["MD5"]))

    def print_failed(self):
        if len(self.failures["HTTP"]):
            failures = "\n".join([f.path for f in self.failures["HTTP"]])
            log.warning("%i files failed to transfer:\n%s" %
                        (len(self.failures["HTTP"]), failures))
            stdoutn("%i files failed to transfer:\n%s" %
                        (len(self.failures["HTTP"]), failures))
        if len(self.failures["MD5"]):
            failures = "\n".join([f.path for f in self.failures["MD5"]])
            log.warning("%i files failed MD5 verification:\n%s" %
                        (len(self.failures["MD5"]), failures))
            stdoutn("%i files failed MD5 verification:\n%s" %
                        (len(self.failures["MD5"]), failures))

    def upload(self, paths=None, title=None, parent="root", checksum=True):
        if not paths:
            paths = []
        if len(paths) > 1:
            title = None  # custom title turned off if more than one file
        for local_file in paths:
            f = GdcpFile(self, path=local_file, parent=parent, checksum=checksum, title=title)
            f.upload()

    def download(self, ids=None, checksum=True, root="."):
        if not ids:
            ids = []
        for _id in ids:
            f = GdcpFile(self, gid=_id, checksum=checksum, root=root)
            f.download()

    def delete(self, ids=None): #CJK added - called by cli_delete
        if not ids:
            ids = []
        for _id in ids:
            f = GdcpFile(self, gid=_id)
            f.delete()

    def move(self, parent, linkIt, ids=None): #CJK added - called by cli_updateParent
        if not ids:
            ids = []
        for _id in ids:
            f = GdcpFile(self, gid=_id)
            f.move(parent,linkIt)

    def copy(self, parent, copy_name, ids=None): #CJK added - called by cli_copy
        if not ids:
            ids = []
        for _id in ids:
            f = GdcpFile(self, gid=_id)
            f.copy(parent,copy_name)

    def list(self, ids=None, json_flag=False, depth=0):
        if not ids:
            ids = []
        for _id in ids:
            f = GdcpFile(self, gid=_id)
            f.list(json_flag=json_flag, depth=depth)

    def transfer_ownership(self, ids, email):
        for _id in ids:
            f = GdcpFile(self, gid=_id)
            f.transfer_ownership(email, root=True)

    def list_all_files(self, json_flag=False):
        """
        Print metadata for all files in Google Drive.
        """
        query = "trashed = false"
        log.debug("query = '%s'" % query)
        request = self.drive.auth.service.files().list(q=query, maxResults=460)
        while request is not None:
            response = execute_request(request)
            for i in response["items"]:
                g = GdcpFile(self, metadata=i)
                g.print_file(json_flag=json_flag)
            request = self.drive.auth.service.files().list_next(request, response)

class GdcpFile(object):

    def __init__(self, gdcp, gid=None, path=None, title=None, parent=None,
        checksum=True, root=None, metadata=None):
        # Assume will construct OK. Set to false if anything part of init
        # fails. This should signal to downstream code to skip this file.
        self.incomplete = False
        self.gdcp = gdcp
        self.drive = gdcp.drive

        self.id = find_id(gid)
        self.path = path
        self.title = title
        self.parent = find_id(parent)
        self.check_checksum = checksum
        self.root = root

        if self.path:
            self.mimetype = guess_mimetype(self.path)
            try:
                self.fileSize = os.path.getsize(self.path)
            except OSError, IOError:
                # Mark as incomplete and ineligible for futher processing
                self.fileSize = 0
                self.incomplete = True
        if self.title is None:
            self.title = title_from_path(self.path)

        self._metadata = None
        self.doctype = None
        self.metadata = metadata

        self.retry_limit = 6
        self.bytes_sent = 0
        self.bytes_received = 0
        self.fail_md5_flag = False
        self.fail_upload_flag = False
        self.fail_download_flag = False
        self.google_md5Checksum = None
        self.local_md5Checksum = None
        self.downloadUrl = None


    def upload(self):
        """
        Recursively upload a file/folder (self.path) to a Google Drive parent
        folder whose ID is self.parent.
        """
        if self.incomplete:
            self._fail_upload()
            return
        # Check for match against any exclude rules
        if not self._passes_excludes():
            return
        # Make sure this is a regular file or directory
        # i.e. not a symlink, socket, named pipe, etc
        if not is_uploadable(self.path):
            return

        log.info("Uploading %s" % self.path)

        if self._is_folder():
            # Folder
            self._create_google_folder()
            self.gdcp.file_count += 1
            allfiles = path_join(self.path, os.listdir(self.path))
            self.gdcp.upload(paths=allfiles, parent=self.id, checksum=self.check_checksum)
        else:
            # File
            media_body = self._create_media_body()
            body = self._create_body()
            t0 = datetime.datetime.now()

            retries = 0
            complete_retries = 0
            response = None

            stdoutn("%s" % self.path)
            stdout("  0.00% 0 0.00MB/s 0s")
            if self.fileSize == 0:
                # zero size files don't do resumable chunked uploads
                request = self.drive.auth.service.files().insert(body=body)
                while response is None:
                    try:
                        response = execute_upload_request(request)
                    except (apiclient.errors.HttpError, KeyError, ssl.SSLError,
                            httplib2.HttpLib2Error, httplib.BadStatusLine, socket.error) as e:
                        # Don't forget that any exceptions caught here should have
                        # been dealt with in backoff decorators for execute_upload_request
                        # too
                        self._fail_upload()
                        break
            else:
                # File is not empty, do resumable chunked upload
                request = self.drive.auth.service.files().insert(body=body, media_body=media_body)
                while response is None:
                    t1 = datetime.datetime.now()
                    try:
                        # Attempt to upload one chunk
                        status, response = request.next_chunk()
                        if status:
                            # Successfully sent a chunk, but download not complete yet
                            # Keep track of progress
                            prev_bytes_sent = self.bytes_sent
                            self.bytes_sent = min(self.bytes_sent + CHUNKSIZE, self.fileSize)
                            cur_progress = status.progress() * 100
                            t_tmp = datetime.datetime.now()
                            rate = calc_transfer_rate(t1, t_tmp, CHUNKSIZE)
                            log.info("Uploaded bytes %i-%i %.02f%% %.02fMB/s" %
                                (prev_bytes_sent + 1, self.bytes_sent, cur_progress, rate))
                            stdoutr("  %.02f%% %i %.02fMB/s %s" %
                                (cur_progress, self.bytes_sent, rate, format_timedelta(t0, t_tmp)))
                    except (apiclient.errors.HttpError, KeyError, ssl.SSLError,
                            httplib2.HttpLib2Error, httplib.BadStatusLine, socket.error, socket.timeout) as e:
                        # Don't forget that any exceptions caught here should have
                        # been dealt with in backoff decorators for execute_upload_request
                        # too

                        # Chunk upload threw an error, retry or restart

                        # Create sensible error string
                        if hasattr(e, "resp"):
                            err_msg = "%s %i" % (type(e).__name__, e.resp.status)
                        else:
                            err_msg = "%s %s" % (type(e).__name__, e)

                        if hasattr(e, "resp") and e.resp.status in [500, 502, 503, 504]:
                            # Retry this chunk
                            if retries < self.retry_limit:
                                log.warning("%s, retrying chunk in %is" % (err_msg, delay(retries)))
                                time.sleep(delay(retries))
                                retries += 1
                            else:
                                # No more retries left, abort
                                log.warning("%s, aborting" % err_msg)
                                self._fail_upload()
                                break
                        else:
                            # Restart upload
                            if complete_retries < self.retry_limit:
                                log.warning("%s, restarting upload in %is" % (err_msg, delay(complete_retries)))
                                time.sleep(delay(complete_retries))
                                self.bytes_sent = 0
                                complete_retries += 1
                                retries = 0
                                request = self.drive.auth.service.files().insert(body=body, media_body=media_body)
                            else:
                                # No more retries left, abort
                                log.warning("%s, aborting" % err_msg)
                                self._fail_upload()
                                break

            self.metadata = response
            if response:
                t2 = datetime.datetime.now()
                rate = calc_transfer_rate(t0, t2, self.fileSize)
                stdoutr("  %.02f%% %i %.02fMB/s %s" %
                    (100.00, self.fileSize, rate, format_timedelta(t0, t2)))
                log.info("Uploaded 100.00%%.  %i bytes in %s %.02fMB/s %s" %
                    (self.fileSize, format_timedelta(t0, t2), rate, self.id))
                if self.check_checksum:
                    self._check_md5()
            stdoutn()
            if self.fail_upload_flag or self.fail_md5_flag:
                log.warning("Upload failed for %s" % self.path)
                stdoutn("  Upload failed")
            else:
                self.gdcp.file_count += 1

        # Do full gc because Python2.7's automatic gc still accumulates more
        # allocated memory than I'd like. Most of the performance hit in this
        # program is network latency so the wall time shouldn't budge
        gc.collect()

    def download(self):
        """
        Recursively download a file/folder to local filesystem starting
        at self.root.
        """
        # Make sure file metadata is present
        self._ensure_google_file_metadata()

        # Check for match against any exclude rules
        if not self._passes_excludes():
            return

        # Skip Google Apps Docs
        if self._is_google_apps_doc():
            return

        if not os.path.exists(self.root):
            self._create_local_folder(path=self.root)

        self.path = os.path.join(self.root, self.title)
        self.path = de_duplicate_path_name(self.path)

        if self._is_folder():
            # Folder
            self._create_local_folder()
            self.gdcp.file_count += 1
            children = self._get_children()
            for f in children:
                f.root = self.path # reset root to be this file's path
                f.download()
        else:
            # File
            stdoutn(self.path)
            stdout("  0.00% 0 0.00MB/s 0s")
            log.info("Downloading %s, size = %i, md5 = %s, id = %s" %
                (self.path, self.fileSize, self.google_md5Checksum, self.id))

            t0 = datetime.datetime.now()
            bytes_start = 0
            bytes_end = min(max(self.fileSize - 1, 0), CHUNKSIZE - 1)

            with open(self.path, "wb") as fh:
                while self.bytes_received < self.fileSize:
                    t1 = datetime.datetime.now()

                    try:
                        log.info("begin dl request for %s at %s" % (self.title, datetime.datetime.now().isoformat()))
                        response, content = execute_download_request(self.drive.auth.service._http,
                            self.downloadUrl, bytes_start, bytes_end)
                        log.info("end dl request for %s at %s" % (self.title, datetime.datetime.now().isoformat()))
                    except (httplib.IncompleteRead, httplib.ResponseNotReady, socket.error, socket.timeout, httplib2.HttpLib2Error) as e:
                        # Don't forget that any exceptions caught here should have
                        # been dealt with in backoff decorators for execute_download_request
                        # too
                        self._fail_download()
                        fh.close()
                        os.remove(self.path)
                        break
                    if response_is_bad([response, content]):
                        self._fail_download()
                        fh.close()
                        os.remove(self.path)
                        break
                    else:
                        fh.write(content)
                        self.bytes_received += bytes_end - bytes_start + 1
                        bytes_this_chunk = bytes_end - bytes_start + 1
                        try:
                            cur_progress = float(self.bytes_received) / self.fileSize * 100
                        except ZeroDivisionError:
                            cur_progress = 100.00
                        t_tmp = datetime.datetime.now()
                        rate = calc_transfer_rate(t1, t_tmp, bytes_this_chunk)
                        log.info("Downloaded bytes %i-%i with status %s %.02f%% %.02fMB/s" %
                            (bytes_start, bytes_end, response.status, cur_progress, rate))
                        stdoutr("  %.02f%% %i %.02fMB/s %s" %
                            (cur_progress, self.bytes_received, rate, format_timedelta(t0, t_tmp)))
                        bytes_start = bytes_end + 1
                        bytes_end = min(max(self.fileSize - 1, 0), bytes_start + CHUNKSIZE - 1)

            t2 = datetime.datetime.now()
            rate = calc_transfer_rate(t0, t2, self.bytes_received)
            try:
                cur_progress = float(self.bytes_received) / self.fileSize * 100
            except ZeroDivisionError:
                cur_progress = 100.00
            if not self.fail_download_flag:
                stdoutr("  %.02f%% %i %.02fMB/s %s" %
                    (cur_progress, self.bytes_received, rate, format_timedelta(t0, t2)))
                if self.check_checksum:
                    if self.check_checksum:
                        self._check_md5()
            stdoutn()
            if self.fail_download_flag or self.fail_md5_flag:
                log.warning("Download failed for %s" % self.path)
                stdoutn("  Download failed for %s" % self.path)
            else:
                rate = calc_transfer_rate(t0, t2, self.fileSize)
                log.info("Downloaded %.02f%% .  %i bytes in %s %.02fMB/s" %
                    (cur_progress, self.bytes_received, format_timedelta(t0, t2), rate))
                self.gdcp.file_count += 1

        # Do full gc because Python2.7's automatic gc still accumulates more
        # allocated memory than I'd like. Most of the performance hit in this
        # program is network latency so the wall time shouldn't budge
        gc.collect()

    def delete(self): #CJK added called by gdcp.delete(...)
        if not self._is_folder():
            request = self.drive.auth.service.files().delete( fileId=self.id )
            resp = execute_request(request)
            #print(resp)
        else:
            print("File is a Folder (not deleting).")

    def copy(self,parent,copy_name): #CJK added called by gdcp.copy(...)
        if not self._is_folder(): #only do this if its a file
            if not copy_name:
                print("No name specified for copied file (not copying)")
                return
            #make a copy of this (self) file (self.id)
            copied_file = {'title': copy_name}
            request = self.drive.auth.service.files().copy( fileId=self.id, body=copied_file )
            resp = execute_request(request)
            newID = resp['id']
            #now move it to the folder passed in (parent)
            gdcp = self.gdcp
            gdcp.move(parent, False, [newID])

    def move(self,parent,linkIt): #CJK added called by gdcp.move(...)
        if not self._is_folder(): #only do this if its a file
            #get the list of the current parent folders
            prevpar = self.metadata["parents"]
            parlist = ""
            for ele in prevpar:
                if parlist != "":
                    parlist += ","
                parlist += "{}".format(str(ele["id"]))
            #now move or link the file using the new parent passed in
            if parlist != "":
                if linkIt: #link the file to another folder
                    request = self.drive.auth.service.files().update( fileId=self.id, addParents=parent )
                else: #move
                    request = self.drive.auth.service.files().update( fileId=self.id, addParents=parent, removeParents=parlist )

                resp = execute_request(request)
            else:
                print("Unable to acquire current parent list (not moving).")
        else:
            print("File is a Folder (not moving).")

    def list(self, json_flag=False, depth=0, predecessors=""):
        """
        Print file listings starting at and including self.

        If depth < -1 do nothing.
        If depth == -1, only print this file.
        If > -1 print this file and if this is a folder call list on each
        child with depth - 1.
        """
        if depth >= 0:
            self.print_file(json_flag, predecessors)
            if self._is_folder():
                children = self._get_children()
                for c in children:
                    if not predecessors:
                        new_pre = self.title
                    else:
                        new_pre = predecessors + "/" + self.title
                    c.list(json_flag, depth-1, new_pre)
        elif depth == -1:
            self.print_file(json_flag, predecessors)

    def print_file(self, json_flag=False, predecessors=""):
        """
        Print metadata for this file
        """
        self._ensure_google_file_metadata()
        if json_flag:
            print json.dumps(self.metadata)
        else:
            line = []
            if predecessors:
                title = predecessors + "/" + remove_r_n(self.metadata["title"])
            else:
                title = remove_r_n(self.metadata["title"])
            line.append(title)
            line.append(self.metadata["id"])
            if self._is_folder():
                file_type = "folder"
            elif self._is_google_apps_doc():
                file_type = self.doctype
            else:
                file_type = "file"
            line.append(file_type)
            if not (self._is_folder() or self._is_google_apps_doc()):
                line.append(self.metadata["fileSize"])
                line.append(self.metadata["md5Checksum"])
            print "\t".join(line)

    def transfer_ownership(self, email, root=True):
        """
        Transfer file ownership to new_owner_email.

        The account for new_owner_email must be in the same Google Apps domain.
        """
        self._ensure_google_file_metadata()

        body = {
            "type": "user",
            "value": email
        }
        if root:
            # Inserting an ownership permission for the top-level folder
            # will send an email and place the folder/file in the new owner's
            # My Drive root folder, unless they already have write permission.
            body["role"] = "owner"
            request = self.drive.auth.service.permissions().insert(
                fileId=self.id,
                body=body)
            insert_resp = execute_request(request)
        else:
            # First make sure writer permission is inserted. Suppress emails.
            # If we just insert an ownership permission here first, and there
            # wasn't already a write permission, the new owner will:
            # 1) receive an email for every file
            # 2) the file will be placed in their My Drive root folder. Neither is
            # desirable.
            body["role"] = "writer"
            request = self.drive.auth.service.permissions().insert(
                fileId=self.id,
                body=body,
                sendNotificationEmails=False)
            insert_resp = execute_request(request)
            log.debug("Granted write privileges for %s" % self.title)
            # Now update permission to owner role. Because the new owner
            # already had write permission an email is not sent and a
            # reference to the file is not placed in My Drive.
            permId = insert_resp["id"]
            body["role"] = "owner"
            request = self.drive.auth.service.permissions().update(
                fileId=self.id,
                permissionId=permId,
                body=body,
                transferOwnership=True)
            execute_request(request)
        self.gdcp.file_count += 1
        stdoutn("%s\t%s" % (self.title, self.id))
        log.info("Transferred ownership of %s" % self.title)

        if self._is_folder():
            children = self._get_children()
            for f in children:
                f.transfer_ownership(email, root=False)

        # Do full gc because Python2.7's automatic gc still accumulates more
        # allocated memory than I'd like. Most of the performance hit in this
        # program is network latency so the wall time shouldn't budge
        gc.collect()

    @property
    def metadata(self):
        return self._metadata

    @metadata.setter
    def metadata(self, response):
        """
        Add response for file metadata from Google Drive

        For response keys/values see return object of get()
        https://developers.google.com/resources/api-libraries/documentation/drive/v2/python/latest/drive_v2.files.html#get
        """
        if response:
            self._metadata = response
            self.id = response["id"]
            self.title = response["title"]
            self.google_md5Checksum = response.get("md5Checksum", None)
            self.downloadUrl = response.get("downloadUrl", None)
            self.fileSize = int(response.get("fileSize", 0))
            self.mimetype = response["mimeType"]
            if self._is_google_apps_doc():
                self.doctype = self._get_google_apps_doctype()

    def _get_children(self):
        """
        Return list of GdcpFile objects for this file's children
        """
        children = []
        query = "trashed = false and '%s' in parents" % self.id
        request = self.drive.auth.service.files().list(q=query, maxResults=460)
        while request != None:
            response = execute_request(request)
            for i in response["items"]:
                g = GdcpFile(self.gdcp) # child inherits Gdcp object
                g.metadata = i
                g.check_checksum = self.check_checksum # child inherits check_checksum
                g.root = self.root # child inherits root
                children.append(g)
            request = self.drive.auth.service.files().list_next(request, response)

        # Sort by title
        children_sorted = sorted(children, key=lambda g: g.title)

        return children_sorted

    def _ensure_google_file_metadata(self):
        """
        Ensure that file metadata from Google is present. Don't perform remote
        API call if file metadata is already present.
        """
        if not self.metadata and self.id:
            log.debug("Ensure fired for %s, %s" % (self.id, self.title))
            request = self.drive.auth.service.files().get(fileId=self.id)
            response = execute_request(request)
            self.metadata = response

    def _create_google_folder(self):
        """
        Create a folder in Google Drive
        """
        body = self._create_body()
        log.debug("About to create folder %s" % body)
        request = self.drive.auth.service.files().insert(body=body)
        response = execute_request(request)
        # Sometimes folders take a while to become available. Try to get metadata
        # from Google to make sure it's available before moving on
        # Skipping here for now, hopefully retries in API calls that use this
        # folder as a parent will be sufficient to hide errors
        #request = self.drive.auth.service.files().get(fileId=response["id"])
        #response = execute_request(request)
        self.metadata = response
        path = self.path if self.path else self.title
        if not path.endswith("/"):
            path += "/"
        stdoutn("%s" % path)
        log.info("Created folder %s %s" % (self.title, self.id))

    def _create_local_folder(self, path=None):
        """
        Create a local folder
        """
        if path:
            folder_path = path
        else:
            folder_path = self.path

        try:
            os.makedirs(folder_path)
        except OSError:
            error("Could not create directory %s. Perhaps it already exists."
                  % folder_path)
        if path:
            log.info("Created folder %s" % folder_path)
        else:
            log.info("Created folder %s %s" % (folder_path, self.id))
        if not folder_path.endswith("/"):
            folder_path += "/"
        stdoutn("%s" % folder_path)

    def _is_folder(self):
        self._ensure_google_file_metadata()
        return self.mimetype == "application/vnd.google-apps.folder"

    def _is_file(self): #CJK added
        self._ensure_google_file_metadata()
        return self.mimetype == "application/vnd.google-apps.file"

    def _is_google_apps_doc(self):
        self._ensure_google_file_metadata()
        if self.mimetype.startswith("application/vnd.google-apps."):
            # Folders are special google apps docs which we consider separately
            if not self._is_folder():
                return True
        return False

    def _get_google_apps_doctype(self):
        if self._is_google_apps_doc():
            return self.mimetype.split("application/vnd.google-apps.")[-1]

    def _create_media_body(self):
        return apiclient.http.MediaFileUpload(self.path,
            chunksize=CHUNKSIZE, resumable=True, mimetype=self.mimetype)

    def _create_body(self):
        body = {"title": self.title}
        if self.mimetype is None:
            body["mimeType"] = "application/vnd.google-apps.folder"
        else:
            body["mimeType"] = self.mimetype
        if self.parent is not None:
            body["parents"] = [{"id": self.parent}]
        return body

    def _check_md5(self):
        """
        Confirm that response MD5 from Google matches MD5 for local
        file
        """
        stdout(" MD5...")
        log.info("Calculating MD5 checksum for %s" % self.path)
        try:
            output = subprocess.check_output(["openssl", "md5", self.path],
                                             stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError:
            error("MD5 calculation exited with an error: '%s'" %
                  output.rstrip())
        self.local_md5Checksum = output.split()[-1]
        if self.local_md5Checksum == self.google_md5Checksum:
            stdout("OK")
            log.info("MD5 OK.  %s (local) == %s" %
                     (self.local_md5Checksum, self.google_md5Checksum))
            return True
        else:
            stdout("FAIL")
            log.warning("MD5 failed.  %s (local) != %s" %
                        (self.local_md5Checksum, self.google_md5Checksum))
            self._fail_md5()
            return False

    def _passes_excludes(self):
        """
        Check if file title passes exclude rules
        """
        if self._is_folder():
            if not self.gdcp.exclude_folders:
                # By default folders always pass
                return True
            # If exlude_folders is True, then apply rules even if this is a
            # folder

        if self.gdcp.include:
            passed = False
        else:
            passed = True
        for regex in self.gdcp.excludes:
            if regex.match(self.title):
                passed = not passed
                break
        return passed

    def _fail_md5(self):
        self.fail_md5_flag = True
        self.gdcp.failures["MD5"].append(self)

    def _fail_upload(self):
        self.fail_upload_flag = True
        self.gdcp.failures["HTTP"].append(self)

    def _fail_download(self):
        self.fail_download_flag = True
        self.gdcp.failures["HTTP"].append(self)


# -----------------------------------------------------------------------------
# Configuration functions
# -----------------------------------------------------------------------------
def configure_logging(stream=None, verbose=False):
    """
    Configure logging handlers
    """
    if stream is None:
        handler = logging.NullHandler()
    else:
        handler = logging.StreamHandler(stream=stream)
    fmt = "%(asctime)-25s %(levelname)-10s %(name)-26s: %(message)s"
    datefmt = "%m/%d/%Y %I:%M:%S %p"
    formatter = logging.Formatter(fmt=fmt, datefmt=datefmt)
    handler.setFormatter(formatter)

    # Logger instances have been created globally
    googleapi.addHandler(handler)
    oauth2_client.addHandler(handler)
    oauth2_util.addHandler(handler)
    googleapi.setLevel(logging.WARNING)
    oauth2_client.setLevel(logging.WARNING)
    # oauth2_util is spitting out warnings like this
    # new_request() takes at most 1 positional argument (6 given)
    # set to error here to ignore
    oauth2_util.setLevel(logging.ERROR)
    backoff_log.addHandler(handler)
    backoff_log.setLevel(logging.INFO)
    log.addHandler(handler)
    log.setLevel(logging.INFO)

    if verbose:
        googleapi.setLevel(logging.INFO)
        oauth2_client.setLevel(logging.INFO)
        oauth2_util.setLevel(logging.INFO)
        log.setLevel(logging.DEBUG)

def configure_signals():
    """
    Signal handling

    Reset SIGINT to default

    Don't raise KeyboardInterrupt exception on SIGINT
    """
    signal.signal(signal.SIGINT, signal.SIG_DFL)

def authorize(location=None):
    if not location:
        location = os.path.join(os.environ["HOME"], "." + PROJ)
    # Authentication
    settings_file = os.path.join(location, "settings.yaml")
    credentials_file = os.path.join(location, "credentials.json")
    tries = 2
    while True:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                gauth = GoogleAuth(settings_file)
                gauth.CommandLineAuth()
                gauth.Authorize()
            break
        except RefreshError as e:
            # Delete credentials, try to get new credentials once as last resort
            sys.stderr.write(str(e) + "\n")
            sys.stderr.write("Attempting to get new access token\n")
            tries -= 1
            if tries == 0:
                raise
            os.remove(credentials_file)

    return gauth

def create_GoogleDrive():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return GoogleDrive(authorize())

def create_configdir(location=None):
    # Create file paths
    if not location:
        location = os.path.join(os.environ["HOME"], "." + PROJ)
    settings = os.path.join(location, "settings.yaml")
    client_secrets = os.path.join(location, "client_secrets.json")
    credentials = os.path.join(location, "credentials.json")

    # Settings file contents
    settings_text = ["client_config_file: %s" % client_secrets]
    settings_text.append("get_refresh_token: True")
    settings_text.append("save_credentials: True")
    settings_text.append("save_credentials_backend: file")
    settings_text.append("save_credentials_file: %s" % credentials)
    if not (CLIENT_ID and CLIENT_SECRET):
        settings_text.append("client_config_backend: file")
    else:
        settings_text.append("client_config_backend: settings")
        settings_text.append("client_config:")
        settings_text.append("  client_id: %s" % CLIENT_ID)
        settings_text.append("  client_secret: %s" % CLIENT_SECRET)

    if not os.path.exists(location):
        os.mkdir(location)
    elif not os.path.isdir(location):
        error("~/.%s already exists and is a file" % PROJ)

    if not os.path.exists(settings):
        try:
            with open(settings, "w") as fh:
                fh.write("\n".join(settings_text) + "\n")
        except (OSError, IOError) as e:
            error("Could not create settings file %s.  %s" %
                  (settings, e))

    if not os.path.exists(client_secrets):
        msg = ["%s not present" % client_secrets, ""]
        msg.append("- Visit https://console.developers.google.com/")
        msg.append("- Create a new project and select it")
        msg.append("- Under 'APIs' make sure the 'Drive API' is turned on")
        msg.append("- Under 'Credentials' create a new OAuth client ID")
        msg.append("  Choose 'Installed -> Other' for application type")
        msg.append("- Click 'Download JSON' to download the secrets file")
        msg.append("- Copy the secrets file to %s" % client_secrets)
        error("\n".join(msg))


# -----------------------------------------------------------------------------
# Utility Functions
# -----------------------------------------------------------------------------
@backoff.on_exception(backoff.expo, apiclient.errors.HttpError, max_tries=6)
@backoff.on_exception(backoff.expo, httplib2.HttpLib2Error, max_tries=6)
@backoff.on_exception(backoff.expo, socket.error, max_tries=6)
@backoff.on_exception(backoff.expo, socket.timeout, max_tries=6)
def execute_request(request):
    return request.execute()

# Don't forget that any exceptions handled here should also be dealt
# with in except where this function is used in case all retries fail
@backoff.on_exception(backoff.expo, apiclient.errors.HttpError, max_tries=6)
@backoff.on_exception(backoff.expo, KeyError, max_tries=6)
@backoff.on_exception(backoff.expo, ssl.SSLError, max_tries=6)
@backoff.on_exception(backoff.expo, httplib.BadStatusLine, max_tries=6)
@backoff.on_exception(backoff.expo, httplib2.HttpLib2Error, max_tries=6)
@backoff.on_exception(backoff.expo, socket.error, max_tries=6)
@backoff.on_exception(backoff.expo, socket.timeout, max_tries=6)
def execute_upload_request(request):
    return request.execute()

def response_is_bad(response):
    """
    Return True if response status is not 206 or 200.

    response is return value from httplib2.http.request GET call, which
    is a two-item list of [response, content]
    """
    bad = not (int(response[0].status) in [206, 200])
    if bad:
        log.warning("Bad status %s" % response[0].status)
    return bad

# Don't forget that any exceptions handled here should also be dealt
# with in except where this function is used in case all retries fail
@backoff.on_exception(backoff.expo, httplib.ResponseNotReady, max_tries=6)
@backoff.on_exception(backoff.expo, httplib.IncompleteRead, max_tries=6)
@backoff.on_exception(backoff.expo, socket.error, max_tries=6)
@backoff.on_exception(backoff.expo, socket.timeout, max_tries=6)
@backoff.on_predicate(backoff.expo, response_is_bad, max_tries=6)
def execute_download_request(h, url, bytes_start, bytes_end):
    """
    Perform GET with httplib2.http object

    Args:
      h = authenticated httplib2.http object
      url = URL to download from
      bytes_start = first byte in range starting at 0
      bytes_end = last byte in range
    """
    headers = {
        "range": "bytes=%i-%i" % (bytes_start, bytes_end),
        "content-type": "application/octet-stream"
    }
    # Returns [response, content]
    return h.request(url, method="GET", headers=headers)

def delay(retries):
    return (2 ** retries) + random.random()

def remove_r_n(some_string):
    some_string = some_string.replace("\r", "^M")
    some_string = some_string.replace("\n", "^M")
    return some_string

def guess_mimetype(file_path):
    if os.path.isdir(file_path):
        mimetype = "application/vnd.google-apps.folder"
    else:
        mimetype = mimetypes.guess_type(file_path)[0]
        if mimetype is None:
            mimetype = "application/octet-stream"
    return mimetype

def path_join(join_path, files):
    """
    Return list of items in files joined to join_path.

    Useful if a common directory prefix needs to be joined to a list of files.
    """
    return map(lambda x: os.path.join(join_path, x), files)

def title_from_path(file_path):
    """
    Get a sanitized title for a file or folder.
    """
    if file_path is None:
        return None
    file_path = os.path.abspath(file_path)  # resolve things like /foo/../foo

    # Special case for root
    if file_path == "/":
        return "/"

    # Remove trailing "/"s
    i = len(file_path) - 1
    while file_path[i] == "/":
        i -= 1
    file_path = file_path[:i+1]

    title = file_path.split("/")[-1]
    return title

def de_duplicate_path_name(old_path):
    """
    Create a sensible new path name when old name is a duplicate.
    """
    if not os.path.exists(old_path):
        return old_path

    delimiter = "_duplicate_"
    old_path = os.path.normpath(old_path)
    head, tail = os.path.split(old_path)
    old_tail_atoms = tail.split(delimiter)

    if delimiter in tail and old_tail_atoms[-1].isdigit():
        i = old_tail_atoms[-1]
        new_tail = tail[0:-len(i)] + str(int(i) + 1)
    else:
        new_tail = tail + delimiter + '1'

    new_path = os.path.join(head, new_tail)

    if os.path.exists(new_path):
        new_path = de_duplicate_path_name(new_path)

    return new_path

def chdir(folder_path):
    try:
        os.chdir(folder_path)
    except OSError:
        error("Could not change to directory %s" % folder_path)

def is_uploadable(path):
    # Test if non-link regular file/dir or link/socket/pipe/device
    if (not os.path.islink(path)) and (os.path.isfile(path) or os.path.isdir(path)):
        return True
    return False

def error(msg):
    log.error(msg)
    sys.stderr.write("\n" + msg + "\n")
    sys.exit(1)

def format_timedelta(t1, t2):
    """
    Format time delta between t2 and t1 as "Nd:Nh:Nm:Ns"
    """
    delta = t2 - t1
    delta_s = delta.total_seconds()
    elapse = []
    days = int(delta_s / (24 * 60 * 60))

    if days > 0:
        elapse.append("%id" % days)
        delta_s -= days * 24 * 60 * 60

    hours = int(delta_s / (60 * 60))
    if hours:
        elapse.append("%ih" % hours)
        delta_s -= hours * 60 * 60

    minutes = int(delta_s / 60)
    if minutes:
        elapse.append("%im" % minutes)
        delta_s -= minutes * 60

    seconds = delta_s
    elapse.append("%.02fs" % seconds)

    return "".join(elapse)


def calc_transfer_rate(t1, t2, file_size):
    """
    Return transfer rate for file_size bytes between t1 and t2
    """
    delta = t2 - t1
    delta_s = delta.total_seconds()
    try:
        rate = float(file_size) / delta_s / 10**6  # MB/s
    except ZeroDivisionError:
        rate = 0.00
    return rate

def find_id(id_string):
    """
    Return file ID from common URLs or URL fragments which contain the ID.

    Valid inputs include:
    - an ID
    - part or all of the Google Drive browser URL for a folder
      e.g. https://drive.google.com/drive/#folders/0B01234567890123456789012345
      e.g. https://drive.google.com/drive/#folders/0B01234567890123456789012345/0B01234567890123456789012346
      e.g. 987012345/0B01234567890123456789012346
    - the download link for a file or folder provided in the Google Drive
      browser interface from "Get Link" in a context menu
      e.g. https://drive.google.com/open?id=0B01234567890123456789012346&authuser=0
      e.g. https://drive.google.com/open?id=0B01234567890123456789012346
    - Google Docs URL
      e.g. https://docs.google.com/a/uw.edu/file/d/0B01234567890123456789012345
      e.g. https://docs.google.com/a/uw.edu/file/d/0B01234567890123456789012345/edit?usp=drivesdk
    - Drive API accessible URL, e.g. one provided by 3rd party tool like insync
      e.g. https://drive.google.com/a/uw.edu/file/d/0B01234567890123456789012345
      e.g. https://drive.google.com/a/uw.edu/file/d/0B01234567890123456789012345/view?usp=drivesdk
    """
    if id_string is None:
        return None
    res = [
        re.compile(r"^([^/]+)$"), # only ID
        re.compile(r"^https://drive\.google\.com/open\?id=([^&]+).*$"), # URL from "Get Link" in context menu
        re.compile(r"^https://drive\.google\.com/drive/#folders/.*/([^/]+)$"), # Google Drive address bar URL for nested folder
        re.compile(r"^https://drive\.google\.com/drive/#folders/([^/]+)$"), # Google Drive address bar URL for one folder
        re.compile(r"^https://(?:docs|drive)\.google\.com/.*/d/([^/]+)$"), # Google Docs/Drive URL
        re.compile(r"^https://(?:docs|drive)\.google\.com/.*/d/([^/]+)/[^/]+$"), # Google Docs/Drive URL with one extra section after ID
        re.compile(r"^.*/([^/]+)") # final catch for part of nested folder URL 987012345/0B01234567890123456789012346
    ]
    match = None
    i = 0
    while match is None and i < len(res):
        match = res[i].match(id_string)
        i += 1
    if match:
        id_string = match.groups()[0]
    return id_string

def parse_id_args(id_args):
    id_strings = []
    for _id in id_args:
        if _id == "-":
            for line in sys.stdin:
                parts = line.rstrip().split()
                id_strings.extend(parts)
        else:
            id_strings.append(_id)
    return id_strings

def parse_file_args(file_args):
    files = []
    for f in file_args:
        if os.path.islink(f):
            sys.stdout.write("Ignoring symbolic link: %s\n" % f)
            log.warning("Ignoring symbolic link: %s" % f)
        elif f == "-":
            for line in sys.stdin:
                line = line.rstrip()
                if line:
                    files.append(line)
        else:
            files.append(f)
    return files

def stdoutr(msg=""):
    """Rewrite current line on STDOUT with no terminating newline"""
    sys.stdout.write("\r%s" % (" " * 79))  # wipe line
    sys.stdout.write("\r%s" % msg)
    sys.stdout.flush()

def stdoutn(msg=""):
    """Write to STDOUT with terminating newline"""
    sys.stdout.write("%s\n" % msg)
    sys.stdout.flush()

def stdout(msg=""):
    """Write to STDOUT with no terminating newline"""
    sys.stdout.write("%s" % msg)
    sys.stdout.flush()

# -----------------------------------------------------------------------------
# Command-line interface functions
# -----------------------------------------------------------------------------
def cli():
    """
    Parse command-line options.
    """
    parent = ArgumentParser(add_help=False)
    parent.add_argument(
        "-l", "--log",
        type=FileType('w'),
        help="""Log file location. Default is no detailed logging.""")
    parent.add_argument(
        "--verbose",
        default=False,
        action="store_true",
        help="Verbose logging output")

    parser = ArgumentParser(
        description="Google Drive command-line interface",
        formatter_class=ArgumentDefaultsHelpFormatter)

    subparsers = parser.add_subparsers(
        dest="subcommand_name",
        help="Sub-command help")

    # Version
    parser_version = subparsers.add_parser(
        "version",
        help="Print the semantic version number",
        parents=[parent])
    parser_version.set_defaults(func=cli_version)

    # List
    parser_list = subparsers.add_parser(
        "list",
        help="""List files in Google Drive. Columns are title, id,
        type (file, folder, doc), fileSize, md5Checksum.  Folders and
        docs only have first 3 columns.""",
        formatter_class=ArgumentDefaultsHelpFormatter,
        parents=[parent])
    parser_list.add_argument(
        "-j", "--json",
        default=False,
        action="store_true",
        help="""Print complete JSON data structure for each file""")
    parser_list.add_argument(
        "-d", "--depth",
        default=0,
        type=int,
        help="""Depth of a recursive listing of files in a folder. 0 only
             lists the file or folder itself. Depth must be >= 0. Using a large
             -d value can be slow for deeply nested directory hierarchies with many
             files.""")
    parser_list.add_argument(
        "-a", "--all",
        default=False,
        action="store_true",
        help="""List all files except for trashed. Much faster than specifying
                a large -d value.""")
    parser_list.add_argument(
        "-i", "--id",
        default=[],
        action="append",
        help="""File or folder ID. If not specified listing starts at Google
                Drive root. - to read a list of IDs from STDIN.""")
    parser_list.set_defaults(func=cli_list)

    # COPY - CJK added
    parser_copy = subparsers.add_parser(
        "copy",
        help="""Copy a file (folders are not supported) in Google Drive. """,
        formatter_class=ArgumentDefaultsHelpFormatter,
        parents=[parent])
    parser_copy.add_argument(
        "-i", "--id",
        default=[],
        action="append",
        help="""File ID. Must be specified.""")
    parser_copy.add_argument(
        "-n", "--copy_name",
        default=None,
        help="""New file name for copy. Must be specified.""")
    parser_copy.add_argument( 
        "-p", "--parent", 
        default = "root",
        help="""Parent ID, i.e. containing folder ID, where to copy the file to. If no ID is specified, the file will be placed in root folder.""")
    parser_copy.set_defaults(func=cli_copy)
    # MOVE - CJK added
    parser_move = subparsers.add_parser(
        "move",
        help="""Move a file (folders are not supported) in Google Drive. """,
        formatter_class=ArgumentDefaultsHelpFormatter,
        parents=[parent])
    parser_move.add_argument(
        "-i", "--id",
        default=[],
        action="append",
        help="""File ID. Must be specified""")
    parser_move.add_argument( 
        "-p", "--parent", 
        default = "root",
        help="""Parent (folder) ID, i.e. containing folder ID, where to move the file to. If no ID is specified, the file will be placed in root folder.""")
    parser_move.add_argument( 
        "-k", "--linkIt", 
        default=False,
        action="store_true",
        help="""Use this argument to link the file to new folder (else move it)""")
    parser_move.set_defaults(func=cli_updateParent)
    # DELETE - CJK added
    parser_delete = subparsers.add_parser(
        "delete",
        help="""Delete a file (folders are not supported) in Google Drive. """,
        formatter_class=ArgumentDefaultsHelpFormatter,
        parents=[parent])
    parser_delete.add_argument(
        "-i", "--id",
        default=[],
        action="append",
        help="""File ID. Must be specified.""")
    parser_delete.set_defaults(func=cli_delete)

    # Download
    parser_download = subparsers.add_parser(
        "download",
        help="Download files from Google Drive. Google Docs are skipped.",
        formatter_class=ArgumentDefaultsHelpFormatter,
        parents=[parent])
    parser_download.add_argument(
        "-i", "--id",
        default=[],
        action="append",
        help="File ID. - to read a list of IDs from STDIN.")
    parser_download.add_argument(
        "-n", "--no_checksum",
        default=False,
        action="store_true",
        help="Skip MD5 checksum verification after download")
    parser_download.add_argument(
        "-e", "--excludes",
        default=[],
        action="append",
        help="""Files with titles matching these Python regular expression
             pattern will be excluded. Does not apply to folders. (See
             --exclude_folders).""")
    parser_download.add_argument(
        "-v", "--invert_excludes",
        default=False,
        action="store_true",
        help="""Change exclude rules to become include rules""")
    parser_download.add_argument(
        "--exclude_folders",
        action="store_true",
        default=False,
        help="""Apply exclude rules to folder titles in addition to file titles""")
    parser_download.add_argument(
        "target",
        help="Destination directory")
    parser_download.set_defaults(func=cli_download)

    # Upload
    parser_upload = subparsers.add_parser(
        "upload",
        help="Upload files to Google Drive",
        formatter_class=ArgumentDefaultsHelpFormatter,
        parents=[parent])
    parser_upload.add_argument(
        "-p", "--parent",
        default="root",
        metavar="ID",
        help="""Parent ID, i.e. containing folder ID. If not specified
             file will be placed in root folder.""")
    parser_upload.add_argument(
        "-n", "--no_checksum",
        default=False,
        action="store_true",
        help="Skip MD5 checksum verification after upload")
    parser_upload.add_argument(
        "-t", "--title",
        help="""Title for file/folder. Must be specified if folder is '.' or
             '..'""")
    parser_upload.add_argument(
        "-e", "--excludes",
        default=[],
        action="append",
        help="""Files with titles matching these Python regular expression
             pattern will be excluded. Does not apply to folders. (See
             --exclude_folders).""")
    parser_upload.add_argument(
        "-v", "--invert_excludes",
        default=False,
        action="store_true",
        help="""Change exclude rules to become include rules""")
    parser_upload.add_argument(
        "--exclude_folders",
        default=False,
        action="store_true",
        help="""Apply exclude rules to folder titles in addition to file titles""")
    parser_upload.add_argument(
        "files",
        nargs="+",
        help="Files/folders to upload. - to read a list of IDs from STDIN.")
    parser_upload.set_defaults(func=cli_upload)

    # Transfer ownership
    parser_transfer = subparsers.add_parser(
        "transfer",
        help="""Transfer ownership of files or folders to a different account
        within the same domain""",
        formatter_class=ArgumentDefaultsHelpFormatter,
        parents=[parent])
    parser_transfer.add_argument(
        "-i", "--id",
        default=[],
        action="append",
        help="File ID. - to read a list of IDs from STDIN.")
    parser_transfer.add_argument(
        "-e",
        "--email",
        help="""Google Apps account email address of new owner""")
    parser_transfer.set_defaults(func=cli_transfer_ownership)

    create_configdir()

    args = parser.parse_args()
    configure_logging(args.log, args.verbose)

    if args.subcommand_name != "version":
        args.drive = create_GoogleDrive()  # add GoogleDrive
    args.func(args)

def cli_list(args):
    ids = parse_id_args(args.id)
    gdcp = Gdcp(args.drive)
    if args.depth < 0:
        error("list -d must be >= 0")
    if args.all:  # start at root
        gdcp.list_all_files(json_flag=args.json)
    else:
        if len(ids) == 0:
            ids.append("root")
        gdcp.list(ids=ids, json_flag=args.json, depth=args.depth)

def cli_delete(args): #CJK added
    ids = parse_id_args(args.id)
    gdcp = Gdcp(args.drive)
    gdcp.delete(ids=ids)

def cli_updateParent(args): #CJK added (for file move)
    ids = parse_id_args(args.id)
    gdcp = Gdcp(args.drive)
    gdcp.move(parent=args.parent,ids=ids,linkIt=args.linkIt)

def cli_copy(args): #CJK added (for file copy)
    ids = parse_id_args(args.id)
    gdcp = Gdcp(args.drive)
    gdcp.copy(parent=args.parent,copy_name=args.copy_name,ids=ids)

def cli_download(args):
    gdcp = Gdcp(args.drive, excludes=args.excludes, include=args.invert_excludes,
        exclude_folders=args.exclude_folders)
    ids = parse_id_args(args.id)
    gdcp.download(ids=ids, checksum=not args.no_checksum, root=args.target)
    if gdcp.failed():
        gdcp.print_failed()
        sys.exit(1)
    else:
        stdoutn("Downloaded %i file(s) and folder(s)" % gdcp.file_count)
        log.info("Downloaded %i file(s) and folder(s)" % gdcp.file_count)

def cli_upload(args):
    gdcp = Gdcp(args.drive, excludes=args.excludes, include=args.invert_excludes,
        exclude_folders=args.exclude_folders)
    files = parse_file_args(args.files)
    gdcp.upload(paths=files, title=args.title, parent=args.parent,
        checksum=not args.no_checksum)
    if gdcp.failed():
        gdcp.print_failed()
        sys.exit(1)
    else:
        stdoutn("Uploaded %i file(s) and folder(s)" % gdcp.file_count)
        log.info("Uploaded %i file(s) and folder(s)" % gdcp.file_count)

def cli_transfer_ownership(args):
    gdcp = Gdcp(args.drive)
    ids = parse_id_args(args.id)
    gdcp.transfer_ownership(ids, args.email)
    log.info("Transferred ownership for %i file(s)" % gdcp.file_count)
    stdoutn("Transferred ownership for %i file(s)" % gdcp.file_count)

def cli_version(args):
    print("%s version %s" % (PROJ, VERSION))

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main():
    configure_signals()
    cli()

if __name__ == "__main__":
    main()

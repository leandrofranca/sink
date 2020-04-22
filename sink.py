#!/usr/bin/env python3

import argparse
import getpass
import hashlib
import http.server
import json
import multiprocessing
import os
import re
import shelve
import socketserver
import sys
import threading
import time
import urllib.parse
import urllib.request
import warnings
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import gdata.contacts.client
import gdata.contacts.data
import gdata.gauth
import mechanicalsoup
from fuzzywuzzy import fuzz, process
from icu import Collator, Locale

# Command descriptions
DESCRIPTION = '''\
Sink is a utility that uses Facebook profile pictures to provide high resolution photos for your Google contacts. \
Sink links each of your Google contacts with their Facebook profile and stores those links to allow for quick updates. \
To learn more about the commands read their help message.'''
UDPATE_DESCRIPTION = '''\
This command updates photos for your Google contacts by using your saved links. \
This command will create links for all contacts without links. \
A link will be automatically created if the contact's name and a Facebook friend name are a close enough match, otherwise you will be prompted to manually create the link. \
To see more detailed instructions run this command.'''
EDIT_DESCRIPTION = '''\
This command interactively edits saved links.
To see more detailed instructions run this command.'''
DELETE_DESCRIPTION = '''\
This command deletes all Google contact photos provided by Sink. \
This command can also delete all saved links.'''
LIST_DESCRIPTION = '''\
This command list all Google contact and Facebook friends provided by Sink.'''

# Instructions
UPDATE_INSTRUCTIONS = '''\
Each unlinked Google contact's name will be displayed along with list of suggested Facebook friends.\n\
You will be presented with a prompt. There are three options.\n\
  1. Type the list number of a suggested Facebook friend and press Enter to create a link with that friend.\n\
  2. Type a name and press Enter to perform another search of Facebook friends.\n\
     This is helpful if a contact's name does not closely match their Facebook name.\n\
  3. Press Enter without typing anything to ignore the contact.\n\
     Sink will ignore this contact during updates.'''
EDIT_INSTRUCTIONS = '''\
You will be presented with a name prompt.  Type the name of the Google contact you wish to edit and press Enter.  Press Enter without typing anything to exit.\n\
If the contact exists, their name will be displayed along with list of suggested Facebook friends.\n\
You will be presented with a prompt. There are three options.\n\
  1. Type the list number of a suggested Facebook friend and press Enter to create a link with that friend.\n\
  2. Type a name and press Enter to perform another search of Facebook friends.\n\
     A link will be automatically created if the search name and a Facebook friend name are a close enough match.\n\
     This is helpful if a contact's name does not closely match their Facebook name.\n\
  3. Press Enter without typing anything to ignore the contact.\n\
     Sink will ignore this contact during updates.'''

# Default arguments
PORT = 7465
SCORE_THRESHOLD = 100
MATCH_LIMIT = 5
RETRIES = 3
DELAY = 0
EXPIRY = 30
COLLATOR = Collator.createInstance(Locale('pt_BR.UTF-8'))
QTY_THREADS = multiprocessing.cpu_count() * 2 + 1

# Shelf keys
TOKEN = 'token'
USERNAME = 'username'
PASSWORD = 'password'
LINKS = 'links'
CHECKSUMS = 'checksums'
TIMESTAMPS = 'timestamps'


class Facebook:
    base_url = 'https://mbasic.facebook.com'
    graph_api_picture = 'https://graph.facebook.com/%s/picture?height=720&width=720&redirect=false'
    user_id_regex = r'/messages/thread/(\d+)'

    def __init__(self, shelf):
        self.browser = mechanicalsoup.StatefulBrowser()
        username = shelf[USERNAME] if USERNAME in shelf else None
        password = shelf[PASSWORD] if PASSWORD in shelf else None
        while(True):
            if username is None:
                username = input("Facebook username: ")
            if password is None:
                password = getpass.getpass("Facebook password: ")
            if self._login(username, password):
                break
            username = None
            password = None
        shelf[USERNAME] = username
        shelf[PASSWORD] = password

    def _login(self, username, password):
        self.browser.open(self.base_url + '/login/')
        login_form = self.browser.select_form()
        login_form.set("email", username)
        login_form.set("pass", password)
        login_form.choose_submit(None)
        self.browser.submit_selected()

        if self._is_checkpoint():
            code = input("Facebook two-factor authentication code: ")
            checkpoint_form = self.browser.select_form()
            checkpoint_form.set('approvals_code', code)
            checkpoint_form.choose_submit(None)
            self.browser.submit_selected()

        if self._is_login_checkpoint():
            remember_form = self.browser.select_form()
            remember_form.set('name_action_selected', 'dont_save')
            remember_form.choose_submit(None)
            response = self.browser.submit_selected()

        if self._is_save_device():
            save_device_form = self.browser.select_form()
            save_device_form.choose_submit(None)
            response = self.browser.submit_selected()

        success = self._is_home()

        if not success:
            print("Login failed. Expected home page but path was %s." % self._get_path())
            answer = input("Do you want to open a view of this page to provide a screenshot for debugging? [y/N]").lower()
            if answer == "y" or answer == "yes":
                self.browser.launch_browser()

        return success

    def _get_path(self):
        return urllib.parse.urlparse(self.browser.get_url()).path

    def _is_home(self):
        path = self._get_path()
        return path == "/" or path == "/home.php"

    def _is_checkpoint(self):
        return self._get_path() == "/checkpoint/"

    def _is_login_checkpoint(self):
        return self._get_path() == "/login/checkpoint/"

    def _is_save_device(self):
        return self._get_path() == "/login/save-device/"

    def get_friends(self):
        friends = {}
        friends_path = '/me/friends'
        while(friends_path):
            self.browser.open(self.base_url + friends_path)
            friend_links = self.browser.links(url_regex='.*fref=fr_tab')
            for link in friend_links:
                href = link.get('href')
                delim = '&' if 'profile.php' in href else '?'
                friends[href.split(delim)[0]] = link.contents[0]
            page_links = self.browser.links(url_regex=r'.*friends\?unit_cursor.*')
            friends_path = page_links[0].get('href') if page_links else None
        friends = dict(sorted(friends.items(), key=lambda kv: (COLLATOR.getSortKey(kv[1]), kv[0])))
        return friends

    def get_user_id(self, friend_url):
        profile_response = self.browser.open(self.base_url + friend_url)
        matcher = re.search(self.user_id_regex, profile_response.text)
        if not matcher:
            return None
        return matcher.group(1)

    def get_profile_picture(self, user_id):
        picture_response = self.browser.open(self.graph_api_picture % user_id)
        picture_data = json.loads(picture_response.text)['data']
        if picture_data['is_silhouette']:
            return None
        return urllib.request.urlretrieve(picture_data['url'])[0]

    def get_profile_url(self, friend_url):
        return "https://m.facebook.com" + friend_url


class GoogleContacts:
    client_id = '89595420582-ggvfe4cufkevtqnvf71posqc177q7mqs.apps.googleusercontent.com'
    client_secret = 'X1CF5RwFBiDCubdmEL3kKxdZ'
    scope = 'https://www.google.com/m8/feeds'
    user_agent = ''
    port = PORT

    def __init__(self, shelf):
        self.client = gdata.contacts.client.ContactsClient()
        if TOKEN not in shelf:
            shelf[TOKEN] = self._get_token()
        token = shelf[TOKEN]
        token.authorize(self.client)

    def _get_token(self):
        server = socketserver.TCPServer(('localhost', self.port), self._OAuthResponseHandler)
        server_thread = threading.Thread(target=server.handle_request)
        server_thread.daemon = True
        server_thread.start()
        token = gdata.gauth.OAuth2Token(client_id=self.client_id, client_secret=self.client_secret, scope=self.scope, user_agent=self.user_agent)
        webbrowser.open(token.generate_authorize_url(redirect_uri='http://localhost:%d' % self.port))
        server_thread.join()
        server.server_close()
        token.get_access_token(server.code)
        return token

    def get_groups(self):
        groups = {}
        feed = self.client.GetGroups()
        for group in feed.entry:
            groups[group.id.text] = group.title.text
        groups = dict(sorted(groups.items(), key=lambda kv: (COLLATOR.getSortKey(kv[1]), kv[0])))
        return groups

    def get_contacts(self):
        contacts = {}
        query = gdata.contacts.client.ContactsQuery(max_results=25000)
        feed = self.client.GetGroups()
        for group in feed.entry:
            if group.system_group and group.system_group.id == 'Contacts':
                query.group = group.id.text
                break
        feed = self.client.GetContacts(q=query)
        for contact in feed.entry:
            if contact.name and contact.name.full_name:
                contacts[contact.id.text.replace('base', 'full')] = contact.name.full_name.text
        contacts = dict(sorted(contacts.items(), key=lambda kv: (COLLATOR.getSortKey(kv[1]), kv[0])))
        return contacts

    def get_group_membership(self):
        group_membership = {}
        query = gdata.contacts.client.ContactsQuery(max_results=25000)
        feed = self.client.GetGroups()
        for group in feed.entry:
            if group.system_group and group.system_group.id == 'Contacts':
                query.group = group.id.text
                break
        feed = self.client.GetContacts(q=query)
        for contact in feed.entry:
            if contact.name and contact.name.full_name:
                group_membership[contact.id.text.replace('base', 'full')] = [group_membership.href for group_membership in contact.group_membership_info]
        return group_membership

    def update_photo(self, contact_url, picture):
        contact = self.client.GetContact(contact_url)
        media = gdata.data.MediaSource(file_path=picture, content_type='image/jpeg')
        self.client.ChangePhoto(media, contact)

    def delete_photo(self, contact_url):
        contact = self.client.GetContact(contact_url)
        self.client.DeletePhoto(media, contact)

    def update_website(self, contact_url, profile_url):
        contact = self.client.GetContact(contact_url)
        if next((s for s in contact.website if s.href == profile_url), None) == None:
            print("UPDATED: %s" % contact.name.full_name.text)
            contact.website = list()
            contact.website.append(gdata.contacts.data.Website(href=profile_url, label=None, primary='false', rel="profile"))
            self.client.Update(contact)

    class _OAuthResponseHandler(http.server.SimpleHTTPRequestHandler):
        html = '''\
<!DOCTYPE html>
<html>
<head>
<title>Sink</title>
</head>
<center>
<p style="font-family:sans-serif; font-size:1.2em; margin:1.5em 0px">Sink permission granted</p>
<p style="font-family:sans-serif; font-size:1em; margin:1em 0px">Please close this page.</p>
</center>
</body>
</html>'''

        def do_GET(self):
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            self.server.code = params['code'][0]
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(self.html.encode('utf-8'))
            self.wfile.flush()

        def log_message(self, format, *args):
            pass


class Sink:
    def __init__(self, shelf):
        self.shelf = shelf
        self.links = self.shelf[LINKS] if LINKS in shelf else {}
        self.checksums = self.shelf[CHECKSUMS] if CHECKSUMS in shelf else {}
        self.timestamps = self.shelf[TIMESTAMPS] if TIMESTAMPS in shelf else {}
        print("Authorizing Google...")
        self.google = GoogleContacts(shelf)
        print("Getting Google groups...")
        self.groups = self.google.get_groups()
        print("Getting Google group membership...")
        self.group_membership = self.google.get_group_membership()
        print("Getting Google contacts...")
        self.contacts = self.google.get_contacts()
        print("%d contacts" % len(self.contacts))
        print("Ordering matches...")
        self.links = dict(sorted(self.links.items(), key=lambda kv: (COLLATOR.getSortKey(self.contacts[kv[0]]), kv[1])))
        print("%d matches" % len([x for x in self.links.values() if x != None]))
        print("Authorizing Facebook...")
        self.facebook = Facebook(shelf)
        print("Getting Facebook friends...")
        self.friends = self.facebook.get_friends()
        print("%d friends" % len(self.friends))

    def update(self, update_ignored=False, auto_only=False, score_threshold=SCORE_THRESHOLD, match_limit=MATCH_LIMIT, retries=RETRIES, delay=DELAY, expiry=EXPIRY):
        self._update_links(update_ignored, auto_only, score_threshold, match_limit)
        self._update_photos(retries, delay, expiry)
        # self._update_fullname()
        # self._update_websites()

    def edit(self, score_threshold=SCORE_THRESHOLD, match_limit=MATCH_LIMIT):
        self._edit_links(score_threshold, match_limit)

    def delete(self, delete_links=False, retries=RETRIES):
        self._delete_photos(retries)
        if delete_links:
            self._delete_links()

    def _get_contact_by_group(self, group):
        return filter(lambda x: self._get_group_by_name(group) in x[1], self.group_membership.items())

    def _get_group_by_name(self, name):
        for id, group in self.groups.items():
            if group == name:
                return id
        return None

    def _update_photos(self, retries, delay, expiry):
        print("Updating photos...")
        pool = ThreadPoolExecutor(QTY_THREADS)
        for contact_url in self.links:
            future = pool.submit(self._update_photo, contact_url, retries, delay, expiry)
            try:
                future.result()
            except StopIteration as cause:
                print(cause)
                break
        pool.shutdown()

    def _update_photo(self, contact_url, retries, delay, expiry):
        friend_url = self.links[contact_url]

        if not friend_url:
            return

        if not self._should_update(contact_url, expiry):
            print("SKIPPED: " + self.contacts[contact_url])
            return

        user_id = self.facebook.get_user_id(friend_url)
        if not user_id:
            raise StopIteration("RATE LIMITED: " + self.contacts[contact_url])

        picture = self.facebook.get_profile_picture(user_id)
        if picture:
            picture_bytes = open(picture, 'rb').read()
            checksum = hashlib.md5(picture_bytes).hexdigest()
            if contact_url in self.checksums and self.checksums[contact_url] == checksum:
                print("UNCHANGED: " + self.contacts[contact_url])
            elif self._retry(lambda: self.google.update_photo(contact_url, picture), retries):
                print("UPDATED: " + self.contacts[contact_url])
                self._set_checksum(contact_url, checksum)
            else:
                print("FAILED: " + self.contacts[contact_url])
            self._set_timestamp(contact_url)
        else:
            print("NO PICTURE: " + self.contacts[contact_url])

        time.sleep(delay)

    def _update_websites(self):
        print("Updating websites...")
        pool = ThreadPoolExecutor(QTY_THREADS)
        [pool.submit(self._update_website, contact_url) for contact_url in self.links]
        pool.shutdown()

    def _update_website(self, contact_url):
        friend_url = self.links[contact_url]
        if friend_url is not None:
            profile_url = self.facebook.get_profile_url(friend_url)
            self.google.update_website(contact_url, profile_url)

    def _update_fullname(self):
        print("Updating full names...")
        for contact_url in self.links:
            friend_url = self.links[contact_url]
            if friend_url is not None:
                friend = self.friends[friend_url]
                contact = self.contacts[contact_url]
                print("Google: %s <-> Facebook: %s" % (contact, friend))

    def _delete_photos(self, retries):
        print("Deleting photos...")
        self._clean_links()
        for contact_url in self.links:
            if self._retry(lambda: self.google.delete_photo(contact_url), retries):
                print("SUCCESS: " + self.contacts[contact_url])
            else:
                print("FAILURE: " + self.contacts[contact_url])

    def _clean_links(self):
        for contact_url in list(self.links.keys()):
            if contact_url not in self.contacts or (self.links[contact_url] is not None and self.links[contact_url] not in self.friends):
                del self.links[contact_url]
                if contact_url in self.checksums:
                    del self.checksums[contact_url]
                if contact_url in self.timestamps:
                    del self.timestamps[contact_url]

    def _update_links(self, update_ignored, auto_only, score_threshold, match_limit):
        print("Updating links...")
        self._clean_links()
        unlinks = []
        for contact_url in self.contacts:
            if contact_url not in self.links or (update_ignored and self.links[contact_url] is None):
                matches = self._get_matches(self.contacts[contact_url], match_limit)
                if matches and matches[0][1] == score_threshold:
                    self._add_link(contact_url, matches[0][2])
                else:
                    unlinks.append(contact_url)
        if not auto_only and unlinks:
            print("\n" + UPDATE_INSTRUCTIONS)
            for contact_url in unlinks:
                print()
                self._get_link(contact_url, score_threshold, match_limit, True)

    def _edit_links(self, score_threshold=SCORE_THRESHOLD, match_limit=MATCH_LIMIT):
        self._clean_links()
        link_contacts = {self.contacts[contact_url]: contact_url for contact_url in self.links}
        print("\n")
        print_columns(sorted(link_contacts.keys(), key=lambda kv: (COLLATOR.getSortKey(kv[0]), kv[1])))
        print("\n" + EDIT_INSTRUCTIONS)
        while(True):
            print()
            name = input("Name: ")
            if not name:
                break
            elif name not in link_contacts:
                print("Invalid name")
            else:
                contact_url = link_contacts[name]
                self._print_link(contact_url, "Status: ")
                self._get_link(contact_url, score_threshold, match_limit, False)

    def _delete_links(self):
        print("Deleting links...")
        self.links.clear()
        self._save_links()

    def _save_links(self):
        self.links = dict(sorted(self.links.items(), key=lambda kv: (COLLATOR.getSortKey(self.contacts[kv[0]]), kv[1])))
        self.shelf[LINKS] = self.links

    def _add_link(self, contact_url, friend_url):
        self.links[contact_url] = friend_url
        self._save_links()
        self._print_link(contact_url)

    def _get_link(self, contact_url, score_threshold, match_limit, auto_match):
        name = self.contacts[contact_url]
        print(name)
        while(True):
            matches = self._get_matches(name, match_limit)
            if auto_match and matches and matches[0][1] == score_threshold:
                self._add_link(contact_url, matches[0][2])
                return
            for i, (name, score, friend_url) in enumerate(matches):
                print("  %d. %s (%d) >> %s" % (i + 1, name, score, friend_url))
            while(True):
                command = input("> ")
                if not command.isdigit() or (int(command) > 0 and int(command) <= match_limit):
                    break
            if not command:
                self._add_link(contact_url, None)
                return
            if command.isdigit():
                self._add_link(contact_url, matches[int(command) - 1][2])
                return
            name = command

    def _print_link(self, contact_url, prefix=""):
        friend_url = self.links[contact_url]
        if friend_url is None:
            print("%s%s IGNORED" % (prefix, self.contacts[contact_url]))
        else:
            print("%s%s <- %s" % (prefix, self.contacts[contact_url], self.friends[friend_url]))

    def _get_matches(self, name, match_limit):
        return process.extract(name, self.friends, scorer=fuzz.UWRatio, limit=match_limit)

    def _should_update(self, contact_url, expiry):
        if contact_url not in self.timestamps:
            return True
        return datetime.now() > self.timestamps[contact_url] + timedelta(days=expiry)

    def _retry(self, func, retries):
        for retry in range(retries):
            try:
                func()
                return True
            except Exception:
                continue
        return False

    def _set_checksum(self, contact_url, checksum):
        self.checksums[contact_url] = checksum
        self.shelf[CHECKSUMS] = self.checksums

    def _set_timestamp(self, contact_url):
        self.timestamps[contact_url] = datetime.now()
        self.shelf[TIMESTAMPS] = self.timestamps


def main():
    args = parse_args()
    shelf = shelve.open(args.filename)
    sink = Sink(shelf)
    if args.command == 'update':
        sink.update(args.update_ignored, args.auto_only, args.score_threshold, args.match_limit, args.retries, args.delay, args.expiry)
    elif args.command == 'edit':
        sink.edit(args.score_threshold, args.match_limit)
    elif args.command == 'delete':
        sink.delete(args.delete_links, args.retries)

def parse_args():
    parser = argparse.ArgumentParser(prog='sink', description=DESCRIPTION, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    subparsers = parser.add_subparsers(dest='command', metavar='command', required=True)
    file_parser = argparse.ArgumentParser(add_help=False)
    file_parser.add_argument('filename', metavar='file', nargs='?', default='sinkshelf', type=filename, help='shelf database file to use')
    update_parser = argparse.ArgumentParser(add_help=False)
    update_parser.add_argument('-a', '--auto-only', dest='auto_only', action='store_true', help='skip all contacts not automatically linked')
    update_parser.add_argument('-i', '--update-ignored', dest='update_ignored', action='store_true', help='update all contacts previously ignored')
    delete_parser = argparse.ArgumentParser(add_help=False)
    delete_parser.add_argument('-l', '--delete-links', dest='delete_links', action='store_true', help='delete saved links')
    param_parser = argparse.ArgumentParser(add_help=False)
    param_parser.add_argument('-s', '--score', dest='score_threshold', metavar='SCORE', default=SCORE_THRESHOLD, type=score, help='score threshold to automatically link contacts')
    param_parser.add_argument('-m', '--matches', dest='match_limit', metavar='MATCHES', default=MATCH_LIMIT, type=int, help='number of matches to show when searching contacts')
    retry_parser = argparse.ArgumentParser(add_help=False)
    retry_parser.add_argument('-r', '--retries', dest='retries', metavar='RETRIES', default=RETRIES, type=int, help='number of times to retry updating photos before failing')
    delay_parser = argparse.ArgumentParser(add_help=False)
    delay_parser.add_argument('-d', '--delay', dest='delay', metavar='DELAY', default=DELAY, type=int, help='number of seconds to wait between contacts when updating photos')
    expiry_parser = argparse.ArgumentParser(add_help=False)
    delay_parser.add_argument('-e', '--expiry', dest='expiry', metavar='EXPIRY', default=EXPIRY, type=int, help='number of days a photo is considered current and should not be updated')
    update = subparsers.add_parser('update', parents=[file_parser, update_parser, param_parser, retry_parser, delay_parser, expiry_parser], description=UDPATE_DESCRIPTION, help='update contact photos', formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    edit = subparsers.add_parser('edit', parents=[file_parser, param_parser], description=EDIT_DESCRIPTION, help='edit contact links', formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    delete = subparsers.add_parser('delete', parents=[file_parser, delete_parser, retry_parser], description=DELETE_DESCRIPTION, help='delete contact photos', formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    return parser.parse_args()

def filename(filename):
    return os.path.splitext(filename)[0]

def score(score):
    score = int(score)
    if score < 0 or score > 100:
        raise argparse.ArgumentTypeError("Score must be between 0 and 100")
    return score

def print_columns(lst):
    for a, b, c in zip(lst[::3], lst[1::3], lst[2::3]):
        print('{:<50}{:<50}{:<}'.format(a, b, c))

if __name__ == "__main__":
    main()

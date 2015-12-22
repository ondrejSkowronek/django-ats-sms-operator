from __future__ import unicode_literals

from datetime import datetime
from itertools import chain

from bs4 import BeautifulSoup
from ipware.ip import get_ip

from django.conf import settings
from django.db import IntegrityError
from django.utils import timezone
from django.utils.encoding import force_text

from is_core.rest.resource import RESTResource

from ats_sms_operator import config


def merge(origin, *args):
    """
    Merges given dictionaries, `origin` will not be changed.
    """
    # TODO remove this once merge is in chamber
    copy = origin.copy()
    for dictionary in args:
        copy.update(dictionary)
    return copy


class InputATSSMSmessageResource(RESTResource):
    login_required = False

    def __init__(self, request, callback_function):
        super(InputATSSMSmessageResource, self).__init__(request)
        self.callback_function = callback_function

    def _deserialize(self):
        soup = BeautifulSoup(force_text(self.request.body), 'html.parser')

        self.request.data = ([merge(msg.attrs, {'content': msg.string}) for msg in soup.messages.find_all('sms')]
                             if soup.messages else ())
        return self.request

    def _serialize(self, result):
        return '\n'.join(chain(
            ('<?xml version="1.0" encoding="UTF-8" ?>', '<status>'),
            ('<code uniq="{}">{}</code>'.format(uniq, code) for code, uniq in result),
            ('</status>',)
        )), 'text/xml'

    def post(self):
        data = self.request.data
        result = []
        for message in data:
            try:
                input_message, created = config.get_input_sms_model().objects.get_or_create(
                    received_at=timezone.make_aware(datetime.strptime(message.get('ts'), "%Y-%m-%d %H:%M:%S"),
                                                    timezone.get_default_timezone()),
                    **{k: v for k, v in message.items()
                       if k in ('uniq', 'sender', 'recipient', 'okey', 'opid', 'opmid', 'content')}
                )
                code = 23 if self.callback_function(input_message, created) else 24
                result.append((code, input_message.uniq))
            except (IntegrityError, TypeError):
                if 'uniq' in message:
                    result.append((24, message.get('uniq')))

        return result

    def has_post_permission(self, *args, **kwargs):
        return (super(InputATSSMSmessageResource, self).has_post_permission(*args, **kwargs) and
                (get_ip(self.request) == config.ATS_SMS_SENDER_IP or getattr(settings, 'ATS_SMS_DEBUG', False)))

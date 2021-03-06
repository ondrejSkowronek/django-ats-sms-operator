from __future__ import unicode_literals

import logging
from itertools import chain

from bs4 import BeautifulSoup

from django.db import models
from django.conf import settings
from django.template import Context, Template
from django.utils import timezone
from django.utils.encoding import force_text
from django.utils.translation import ugettext

from chamber.shortcuts import get_object_or_none

from ats_sms_operator import config, logged_requests as requests


LOGGER = logging.getLogger('ats_sms')

header = """<?xml version="1.0" encoding="UTF-8" ?>
            <messages>
                <auth>
                    <name>{username}</name>
                    <password>{password}</password>
                </auth>"""
footer = '</messages>'


class DeliveryRequest(object):
    """
    Helper class to create ATS delivery requests from an output SMS. Since the output SMS itself must implement
    serialize_ats() method as well.
    """

    def __init__(self, output_sms):
        self.output_sms = output_sms

    def serialize_ats(self):
        return """<dlr uniq="{}">{}</dlr>""".format(self.output_sms.pk, self.output_sms.pk)


class ATSSMSException(Exception):
    pass


class SMSSendingError(ATSSMSException):
    pass


class SMSValidationError(ATSSMSException):
    pass


def serialize_ats_requests(*ats_serializable_objects):
    """
    Prepares XML with the given ATS elementary requests. The requests must be an instance of a class implementing
    the serialize_ats() method.
    """
    not_serializable = set(request.__class__.__name__ for request in ats_serializable_objects
                           if not hasattr(request, 'serialize_ats'))
    if not_serializable:
        raise SMSSendingError(
            ugettext('Passed classes do not implement serialize_ats() method: {}').format(not_serializable)
        )

    return ''.join(chain(
        (header.format(username=config.ATS_USERNAME, password=config.ATS_PASSWORD),),
        (request.serialize_ats() for request in ats_serializable_objects),
        (footer,),
    ))


def send_ats_requests(*ats_serializable_objects):
    """
    Performs the actual POST request with the given elementary ATS requests.
    """
    requests_xml = serialize_ats_requests(*ats_serializable_objects)
    logged_requests = [request for request in ats_serializable_objects if isinstance(request, models.Model)]
    try:
        return requests.post(config.ATS_URL, data=requests_xml, headers={'Content-Type': 'text/xml'},
                             slug='ATS SMS', related_objects=logged_requests)
    except requests.exceptions.RequestException as e:
        raise SMSSendingError(str(e))


def parse_response_codes(xml):
    """
    Finds all <code> tags in the given XML and returns a mapping "uniq" -> "response code" for all SMS.
    In case of an error, the error is logged.
    """
    soup = BeautifulSoup(xml, 'html.parser')
    code_tags = soup.find_all('code')

    LOGGER.warning(', '.join(
        [force_text(config.ATS_STATES.get_label(c)) if c in config.ATS_STATES.all else 'ATS returned an unknown state {}.'.format(c)
         for c in [int(error_code.string) for error_code in code_tags if not error_code.attrs.get('uniq')]],
    ))

    return {int(code.attrs['uniq']): int(code.string) for code in code_tags if code.attrs.get('uniq')}


def send_and_parse_response(*ats_requests):
    """
    Glue function to perform sending ATS requests and parsing the ATS server response in one go.
    """
    return parse_response_codes(send_ats_requests(*ats_requests).text)


def send_and_update_sms_states(*ats_requests):
    """
    Higher-level function performing serialization of ATS requests, parsing ATS server response and updating
    SMS messages state according the received response.
    """
    for uniq, state in send_and_parse_response(*ats_requests).items():
        sms = get_object_or_none(config.get_output_sms_model(), pk=uniq)
        if sms:
            sms.state = state if state in config.ATS_STATES.all else config.ATS_STATES.LOCAL_UNKNOWN_ATS_STATE
            sms.sent_at = timezone.now()
            sms.save()
        else:
            raise SMSValidationError(ugettext('SMS with uniq "{}" not found in DB.').format(uniq))


def send_template(recipient, slug='', context=None, **sms_attrs):
    """
    Use this function to send a SMS template to a given number.
    """
    context = context or {}
    try:
        sms_template = config.get_sms_template_model().objects.get(slug=slug)
        output_sms = config.get_output_sms_model().objects.create(
            recipient=recipient,
            content=Template(sms_template.body).render(Context(context)),
            state=config.ATS_STATES.DEBUG if settings.ATS_SMS_DEBUG else config.ATS_STATES.LOCAL_TO_SEND,
            **sms_attrs
        )
        return output_sms
    except config.get_sms_template_model().DoesNotExist:
        LOGGER.error(ugettext('SMS message template with slug {slug} does not exist. '
                              'The message to {recipient} cannot be sent.').format(recipient=recipient, slug=slug))
        raise SMSSendingError(ugettext('SMS message template with slug {} does not exist').format(slug))

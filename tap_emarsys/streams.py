import time
from functools import partial

import singer
import pendulum
import requests
from singer import metadata
from singer.bookmarks import write_bookmark, clear_bookmark
from ratelimit import limits, sleep_and_retry, RateLimitException
from backoff import on_exception, expo

from .schemas import (
    IDS,
    get_contacts_raw_fields,
    get_contact_field_type,
    normalize_fieldname,
    METRICS_AVAILABLE
)

LOGGER = singer.get_logger()

def metrics(tap_stream_id, records):  
    with singer.metrics.record_counter(tap_stream_id) as counter:
        counter.increment(len(records))

def write_records(tap_stream_id, records):
    singer.write_records(tap_stream_id, records)
    metrics(tap_stream_id, records)

def base_transform(obj, date_fields):
    new_obj = {}
    for field, value in obj.items():
        if value == '':
            value = None
        elif field in date_fields and value is not None:
            value = pendulum.parse(value).isoformat()
        new_obj[field] = value
    return new_obj

def sync_campaigns(ctx, sync):
    data = ctx.client.get('/email/', tap_stream_id='campaigns', params={
        'showdeleted': 1
    })
    def campaign_transformed(campaign):
        return base_transform(campaign, ['created', 'deleted'])
    data_transformed = list(map(campaign_transformed, data))
    if sync:
        ## TODO: select fields?
        write_records('campaigns', data_transformed)
    return data_transformed

def transform_contact(field_id_map, contact):
    new_obj = {}
    for field_id, value in contact.items():
        if field_id in ['id', 'uid']:
            new_obj[field_id] = value
            continue
        field_info = field_id_map[field_id]
        if value == '':
            value = None
        elif field_info['type'] == 'date':
            value = pendulum.parse(value).isoformat()
        new_obj[field_info['name']] = value
    return new_obj

def paginate_contacts(ctx, field_id_map, selected_fields, limit=1000, offset=0):
    contact_list_page = ctx.client.get('/contact/query/', params={
        'return': 3,
        'limit': limit,
        'offset': offset
    })
    if len(contact_list_page['errors']) > 0:
        raise Exception('contacts - {}'.format(','.join(contact_list_page['errors'])))

    query = {
        'keyId': 'id',
        'keyValues': list(map(lambda x: x['id'], contact_list_page['result'])),
        'fields': list(map(lambda x: x['id'], selected_fields))
    }
    contact_page = ctx.client.post('/contact/getdata', query, tap_stream_id='contacts')

    contacts = list(map(partial(transform_contact, field_id_map), contact_page['result']))
    write_records('contacts', contacts)

    if len(contact_page['result']) == limit:
        paginate_contacts(ctx, field_id_map, selected_fields, limit=limit, offset=offset + limit)

def sync_contacts(ctx):
    contacts_stream = ctx.catalog.get_stream('contacts')

    raw_fields = get_contacts_raw_fields(ctx)
    field_name_map = {}
    field_id_map = {}
    for raw_field in raw_fields:
        field_id = str(raw_field['id'])
        field_name = normalize_fieldname(raw_field['name'])
        field_info = {
            'type': raw_field['application_type'],
            'name': field_name,
            'id': field_id
        }
        field_name_map[field_name] = field_info
        field_id_map[field_id] = field_info
    raw_fields_available = list(field_name_map.keys())

    selected_fields = []
    for prop, schema in contacts_stream.schema.properties.items():
        if schema.selected == True:
            if prop not in raw_fields_available:
                raise Exception('Field `{}` not currently available from Emarsys'.format(
                    prop))
            selected_fields.append(field_name_map[prop])

    paginate_contacts(ctx, field_id_map, selected_fields)

def sync_contact_lists(ctx, sync):
    data = ctx.client.get('/contactlist', tap_stream_id='contact_lists')
    ## TODO: select fields?
    def contact_list_transform(contact_list):
        return base_transform(contact_list, ['created'])
    data_transformed = list(map(contact_list_transform, data))
    if sync:
        write_records('contact_lists', data_transformed)
    return data_transformed

def sync_contact_list_memberships(ctx, contact_list_id, limit=1000000, offset=0):
    membership_ids = ctx.client.get('/contactlist/{}/'.format(contact_list_id),
                                    params={
                                        'limit': limit,
                                        'offset': offset
                                    },
                                    tap_stream_id='contact_list_memberships')
    memberships = []
    for membership_id in membership_ids:
        memberships.append({
            'contact_list_id': contact_list_id,
            'contact_id': membership_id
        })
    write_records('contact_list_memberships', memberships)

    if len(membership_ids) == limit:
        sync_contact_list_memberships(ctx, contact_list_id, limit=limit, offset=offset + limit)

def sync_contact_lists_memberships(ctx, contact_lists):
    for contact_list in contact_lists:
        sync_contact_list_memberships(ctx, contact_list['id'])

@on_exception(expo, RateLimitException, max_tries=5)
@sleep_and_retry
@limits(calls=1, period=61) # 60 seconds needed to be padded by 1 second to work
def post_metric(ctx, metric, date, campaign_id):
    return ctx.client.post('/email/responses', {
        'type': metric,
        'start_date': date,
        'end_date': date,
        'campaign_id': campaign_id
    })

def sync_metric(ctx, campaign_id, metric, date):
    ## TODO: job metrics
    job = post_metric(ctx, metric, date, campaign_id)

    num_attempts = 0
    while num_attempts < 10:
        num_attempts += 1
        data = ctx.client.get('/email/{}/responses'.format(job['id']))
        if data != '':
            break
        else:
            time.sleep(5)

    if len(data['contact_ids']) == 1 and data['contact_ids'][0] == '':
        return

    data_rows = []
    for contact_id in data['contact_ids']:
        data_rows.append({
            'date': date,
            'metric': metric,
            'contact_id': contact_id
        })

    write_records('metrics', data_rows)

def write_metrics_state(ctx, campaigns_to_resume, metrics_to_resume, date_to_resume):
    write_bookmark(ctx.state, 'metrics', 'campaigns_to_resume', campaigns_to_resume)
    write_bookmark(ctx.state, 'metrics', 'metrics_to_resume', metrics_to_resume)
    write_bookmark(ctx.state, 'metrics', 'date_to_resume', date_to_resume.to_date_string())
    ctx.write_state()

def sync_metrics(ctx, campaigns):
    stream = ctx.catalog.get_stream('metrics')
    bookmark = ctx.state.get('bookmarks', {}).get('metrics', {})

    if stream.metadata:
        mdata = metadata.to_map(stream.metadata)
        metrics_selected = (
            mdata
            .get((), {})
            .get('tap-emarsys.metrics-selected', METRICS_AVAILABLE)
        )
    else:
        metrics_selected = METRICS_AVAILABLE

    start_date = pendulum.parse(ctx.config.get('start_date', 'now'))
    end_date = pendulum.parse(ctx.config.get('end_date', 'now'))

    start_date = bookmark.get('last_metric_date', start_date)

    campaigns_to_resume = bookmark.get('campaigns_to_resume')
    if campaigns_to_resume:
        campaign_ids = campaigns_to_resume
        campaign_metrics = bookmark.get('metrics_to_resume')
        last_date = bookmark.get('date_to_resume')
        if last_date:
            last_date = pendulum.parse(last_date)
    else:
        campaign_ids = (
            list(map(lambda x: x['id'],
                     filter(lambda x: x['deleted'] == None,
                            campaigns)))
        )
        campaign_metrics = metrics_selected
        last_date = None

    campaigns_to_resume = campaign_ids.copy()
    for campaign_id in campaign_ids:
        metrics_to_resume = metrics_selected.copy()
        for metric in campaign_metrics:
            current_date = last_date or start_date
            last_date = None
            while current_date <= end_date:
                sync_metric(ctx, campaign_id, metric, current_date.to_date_string())
                current_date = current_date.add(days=1)
                date_to_resume = current_date ## TODO: can be greaterthan end_date
                write_metrics_state(ctx, campaigns_to_resume, metrics_to_resume, date_to_resume)
            date_to_resume = None
            metrics_to_resume.remove(metric)
        campaigns_to_resume.remove(campaign_id)
        campaign_metrics = metrics_selected

    reset_stream(ctx.state, 'metrics')
    write_bookmark(ctx.state, 'metrics', 'last_metric_date', end_date.to_date_string())
    ctx.write_state()

def sync_selected_streams(ctx):
    selected_streams = ctx.selected_stream_ids
    last_synced_stream = ctx.state.get('last_synced_stream')

    if IDS.CONTACTS in selected_streams and last_synced_stream != IDS.CONTACTS:
        sync_contacts(ctx)
        ctx.state['last_synced_stream'] = IDS.CONTACTS
        ctx.write_state()

    if (IDS.CONTACT_LISTS in selected_streams and
        last_synced_stream != IDS.CONTACT_LISTS) or \
       (IDS.CONTACT_LIST_MEMBERSHIPS in selected_streams and
        last_synced_stream != IDS.CONTACT_LIST_MEMBERSHIPS):
        contact_lists = sync_contact_lists(ctx, IDS.CONTACT_LISTS in selected_streams)
        ctx.state['last_synced_stream'] = IDS.CONTACT_LISTS
        ctx.write_state()

    if IDS.CONTACT_LIST_MEMBERSHIPS in selected_streams and \
       last_synced_stream != IDS.CONTACT_LIST_MEMBERSHIPS:
        sync_contact_lists_memberships(ctx, contact_lists)
        ctx.state['last_synced_stream'] = IDS.CONTACT_LIST_MEMBERSHIPS
        ctx.write_state()

    if (IDS.CAMPAIGNS in selected_streams and
        last_synced_stream != IDS.CAMPAIGNS) or \
       (IDS.METRICS in selected_streams and
        last_synced_stream != IDS.METRICS):
        campaigns = sync_campaigns(ctx, IDS.CAMPAIGNS in selected_streams)
        ctx.state['last_synced_stream'] = IDS.CAMPAIGNS
        ctx.write_state()

    if IDS.METRICS in selected_streams and last_synced_stream != IDS.METRICS:
        sync_metrics(ctx, campaigns)
        ctx.state['last_synced_stream'] = IDS.METRICS
        ctx.write_state()

    ctx.state['last_synced_stream'] = None
    ctx.write_state()   

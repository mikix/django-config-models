"""
Django Model baseclass for database-backed configuration.
"""
from __future__ import absolute_import, unicode_literals

from django.db import connection, models
from django.contrib.auth.models import User
from django.core.cache import caches, InvalidCacheBackendError
from django.utils.translation import ugettext_lazy as _

from rest_framework.utils import model_meta

# Config model values are cached. The caching backend configuration used has implications for how the values
# are changed/queried in an active release. When a new key/value is saved, its current cached value is cleared
# via deletion from the cache. Otherwise, depending on the cache backend configuration *and* on the value of
# ConfigurationModel.cache_timeout, the key/value may still exist in some caches.
try:
    # Use the Django settings-configured cache, which could be:
    #  - memcache or another external networked cache (per-cluster)
    #  - file cache (per-machine)
    #  - local memory cache (per-process)
    #  - other
    cache = caches['configuration']  # pylint: disable=invalid-name
except InvalidCacheBackendError:
    # If no caches configured, use the default cache specified in Django CACHES setting.
    from django.core.cache import cache  # pylint: disable=ungrouped-imports


class ConfigurationModelManager(models.Manager):
    """
    Query manager for ConfigurationModel
    """
    def _current_ids_subquery(self):
        """
        Internal helper method to return an SQL string that will get the IDs of
        all the current entries (i.e. the most recent entry for each unique set
        of key values). Only useful if KEY_FIELDS is set.
        """
        key_fields_escaped = [connection.ops.quote_name(name) for name in self.model.KEY_FIELDS]
        # The following assumes that the rows with the most recent date also have the highest IDs
        return "SELECT MAX(id) FROM {table_name} GROUP BY {key_fields}".format(
            key_fields=', '.join(key_fields_escaped),
            table_name=self.model._meta.db_table  # pylint: disable=protected-access
        )

    def current_set(self):
        """
        A queryset for the active configuration entries only. Only useful if KEY_FIELDS is set.

        Active means the means recent entries for each unique combination of keys. It does not
        necessaryily mean enbled.
        """
        assert self.model.KEY_FIELDS != (), "Just use model.current() if there are no KEY_FIELDS"
        return self.get_queryset().extra(           # pylint: disable=no-member
            where=["{table_name}.id IN ({subquery})".format(
                table_name=self.model._meta.db_table,  # pylint: disable=protected-access, no-member
                subquery=self._current_ids_subquery(),
            )],
            select={'is_active': 1},  # This annotation is used by the admin changelist. sqlite requires '1', not 'True'
        )

    def with_active_flag(self):
        """
        A query set where each result is annotated with an 'is_active' field that indicates
        if it's the most recent entry for that combination of keys.
        """
        if self.model.KEY_FIELDS:
            subquery = self._current_ids_subquery()
            return self.get_queryset().extra(           # pylint: disable=no-member
                select={'is_active': "{table_name}.id IN ({subquery})".format(
                    table_name=self.model._meta.db_table,  # pylint: disable=protected-access, no-member
                    subquery=subquery,
                )}
            )
        return self.get_queryset().extra(           # pylint: disable=no-member
            select={'is_active': "{table_name}.id = {pk}".format(
                table_name=self.model._meta.db_table,  # pylint: disable=protected-access, no-member
                pk=self.model.current().pk,  # pylint: disable=no-member
            )}
        )


class ConfigurationModel(models.Model):
    """
    Abstract base class for model-based configuration

    Properties:
        cache_timeout (int): The number of seconds that this configuration
            should be cached
    """

    class Meta(object):
        abstract = True
        ordering = ("-change_date", )

    objects = ConfigurationModelManager()

    KEY_FIELDS = ()

    # The number of seconds
    cache_timeout = 600

    change_date = models.DateTimeField(auto_now_add=True, verbose_name=_("Change date"))
    changed_by = models.ForeignKey(
        User,
        editable=False,
        null=True,
        on_delete=models.PROTECT,
        # Translators: this label indicates the name of the user who made this change:
        verbose_name=_("Changed by"),
    )
    enabled = models.BooleanField(default=False, verbose_name=_("Enabled"))

    def save(self, force_insert=False, force_update=False, using=None,
             update_fields=None):
        """
        Clear the cached value when saving a new configuration entry
        """
        # Always create a new entry, instead of updating an existing model
        self.pk = None  # pylint: disable=invalid-name
        super(ConfigurationModel, self).save(
            force_insert,
            force_update,
            using,
            update_fields
        )
        cache.delete(self.cache_key_name(*[getattr(self, key) for key in self.KEY_FIELDS]))
        if self.KEY_FIELDS:
            cache.delete(self.key_values_cache_key_name())

    @classmethod
    def cache_key_name(cls, *args):
        """Return the name of the key to use to cache the current configuration"""
        if cls.KEY_FIELDS != ():
            if len(args) != len(cls.KEY_FIELDS):
                raise TypeError(
                    "cache_key_name() takes exactly {} arguments ({} given)".format(len(cls.KEY_FIELDS), len(args))
                )
            # pylint: disable=unicode-builtin
            return 'configuration/{}/current/{}'.format(cls.__name__, ','.join(unicode(arg) for arg in args))
        else:
            return 'configuration/{}/current'.format(cls.__name__)

    @classmethod
    def current(cls, *args):
        """
        Return the active configuration entry, either from cache,
        from the database, or by creating a new empty entry (which is not
        persisted).
        """
        cached = cache.get(cls.cache_key_name(*args))
        if cached is not None:
            return cached

        key_dict = dict(zip(cls.KEY_FIELDS, args))
        try:
            current = cls.objects.filter(**key_dict).order_by('-change_date')[0]
        except IndexError:
            current = cls(**key_dict)

        cache.set(cls.cache_key_name(*args), current, cls.cache_timeout)
        return current

    @classmethod
    def is_enabled(cls, *key_fields):
        """
        Returns True if this feature is configured as enabled, else False.

        Arguments:
            key_fields: The positional arguments are the KEY_FIELDS used to identify the
                configuration to be checked.
        """
        return cls.current(*key_fields).enabled

    @classmethod
    def key_values_cache_key_name(cls, *key_fields):
        """ Key for fetching unique key values from the cache """
        key_fields = key_fields or cls.KEY_FIELDS
        return 'configuration/{}/key_values/{}'.format(cls.__name__, ','.join(key_fields))

    @classmethod
    def key_values(cls, *key_fields, **kwargs):
        """
        Get the set of unique values in the configuration table for the given
        key[s]. Calling cls.current(*value) for each value in the resulting
        list should always produce an entry, though any such entry may have
        enabled=False.

        Arguments:
            key_fields: The positional arguments are the KEY_FIELDS to return. For example if
                you had a course embargo configuration where each entry was keyed on (country,
                course), then you might want to know "What countries have embargoes configured?"
                with cls.key_values('country'), or "Which courses have country restrictions?"
                with cls.key_values('course'). You can also leave this unspecified for the
                default, which returns the distinct combinations of all keys.
            flat: If you pass flat=True as a kwarg, it has the same effect as in Django's
                'values_list' method: Instead of returning a list of lists, you'll get one list
                of values. This makes sense to use whenever there is only one key being queried.

        Return value:
            List of lists of each combination of keys found in the database.
            e.g. [("Italy", "course-v1:SomeX+some+2015"), ...] for the course embargo example
        """
        flat = kwargs.pop('flat', False)
        assert not kwargs, "'flat' is the only kwarg accepted"
        key_fields = key_fields or cls.KEY_FIELDS
        cache_key = cls.key_values_cache_key_name(*key_fields)
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        values = list(cls.objects.values_list(*key_fields, flat=flat).order_by().distinct())
        cache.set(cache_key, values, cls.cache_timeout)
        return values

    def fields_equal(self, instance, fields_to_ignore=("id", "change_date", "changed_by")):
        """
        Compares this instance's fields to the supplied instance to test for equality.
        This will ignore any fields in `fields_to_ignore`.

        Note that this method ignores many-to-many fields.

        Args:
            instance: the model instance to compare
            fields_to_ignore: List of fields that should not be compared for equality. By default
            includes `id`, `change_date`, and `changed_by`.

        Returns: True if the checked fields are all equivalent, else False
        """
        for field in self._meta.get_fields():
            if not field.many_to_many and field.name not in fields_to_ignore:
                if getattr(instance, field.name) != getattr(self, field.name):
                    return False

        return True

    @classmethod
    def equal_to_current(cls, json, fields_to_ignore=("id", "change_date", "changed_by")):
        """
        Compares for equality this instance to a model instance constructed from the supplied JSON.
        This will ignore any fields in `fields_to_ignore`.

        Note that this method cannot handle fields with many-to-many associations, as those can only
        be set on a saved model instance (and saving the model instance will create a new entry).
        All many-to-many field entries will be removed before the equality comparison is done.

        Args:
            json: json representing an entry to compare
            fields_to_ignore: List of fields that should not be compared for equality. By default
            includes `id`, `change_date`, and `changed_by`.

        Returns: True if the checked fields are all equivalent, else False
        """

        # Remove many-to-many relationships from json.
        # They require an instance to be already saved.
        info = model_meta.get_field_info(cls)
        for field_name, relation_info in info.relations.items():
            if relation_info.to_many and (field_name in json):
                json.pop(field_name)

        new_instance = cls(**json)
        key_field_args = tuple(getattr(new_instance, key) for key in cls.KEY_FIELDS)
        current = cls.current(*key_field_args)
        # If current.id is None, no entry actually existed and the "current" method created it.
        if current.id is not None:
            return current.fields_equal(new_instance, fields_to_ignore)

        return False

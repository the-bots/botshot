import logging
from typing import Optional
from django.conf import settings
from botshot.core.context import Context
from botshot.core.dialog import Dialog
from botshot.core.flow import Flow, State
from botshot.core.responses import TextMessage
from botshot.models import ChatMessage
from botshot.core import config

class MessageProcessor:

    def __init__(self, flows, message, chat_manager):
        from botshot.core.chat_manager import ChatManager
        self.message: ChatMessage = message
        self.chat_manager: ChatManager = chat_manager
        self.send_exceptions = config.get("SEND_EXCEPTIONS", default=settings.DEBUG)
        if flows is None:
            raise ValueError("Flows have not been initialized.")
        self.flows = flows
        self.current_state_name = self.message.user.conversation.state or 'default.root'
        self.context = Context.from_dict(dialog=self, data=message.user.conversation.context_dict or {})
        self.dialog = Dialog(message=self.message, context=self.context, chat_manager=self.chat_manager)

    def process(self):
        accepted_state = self.current_state_name

        # TODO don't increment when @ requires -> input and it's valid
        # TODO what to say and do on invalid requires -> input?
        self.context.counter += 1

        self.context.add_entities(self.message.entities)
        self.context.debug()

        logging.debug('>>> Processing message')

        #logging_service.log_user_message.delay(session=self.session, message=message, entities=self.message.entities,
        #                                       state=accepted_state)

        if self._special_message(self.message.text):
            return
        if self._check_state_transition():
            return
        if self._check_intent_transition(self.message.entities):
            return
        if self._check_entity_transition(self.message.entities):
            return

        entity_values = self._get_entity_value_tuples(self.message.entities)
        if self.get_state().is_supported(entity_values):
            logging.debug("Entity supported, no entity transition")
            self._run_accept()
            return

        # run 'unsupported' action of the state
        if self.get_state().unsupported:
            self._run_action(self.get_state().unsupported)
        # if not provided, run 'unsupported' action of the flow
        elif self.get_flow().unsupported:
            self._run_action(self.get_flow().unsupported)
        # if not provided, give up and go to default.root
        else:
            self._move_to("default.root:")

        self.message.user.conversation.state = self.current_state_name
        self.message.user.conversation.context_dict = self.context.to_dict()

    def _special_message(self, text):
        if not text:
            return False
        if text == '/areyoubotshot':
            from botshot import __version__
            self.dialog.send("Botshot Framework version {}".format(__version__))
            return True
        return False

    def _run_accept(self):
        """Runs action of the current state."""
        state = self.get_state()
        if self.current_state_name != 'default.root' and not state.check_requirements(self.context):
            requirement = state.get_first_requirement(self.context)
            self._run_action(requirement.action)
            return
        if not state.action:
            logging.warning('State {} does not have an action.'.format(self.current_state_name))
            return
        self._run_action(state.action)

    def _run_action(self, fn):
        if not callable(fn):
            logging.error("Error: Trying to run a function of type {}".format(type(fn)))
            return
        # run the action
        retval = fn(dialog=self.dialog)
        # send a response if given in return value
        if retval and not isinstance(retval, str):
            raise ValueError("Error: Action must return either None or a state name.")
        self._move_to(retval)

    def _check_state_transition(self):
        """Checks if entity _state was received in current message (and moves to the state)"""
        new_state_name = self.context.get_value(entity='_state', max_age=0)
        if new_state_name is not None:
            return self._move_to(new_state_name)
        return False

    def _check_intent_transition(self, entities: dict):
        """Checks if intent was parsed from current message (and moves by intent)"""
        intent = self.context.intent.get_value(this_msg=True)
        if not intent:
            return False

        entity_values = self._get_entity_value_tuples(entities, include=("intent", "_message_text"))
        if self.get_state().is_supported(entity_values):
            logging.debug("Intent or text supported, no intent transition")
            return False

        # move to the flow whose 'intent' field matches intent

        # Check accepted intent of the current flow's states
        flow = self.get_flow()
        new_state_name = flow.get_state_for_intent(intent)

        # Check accepted intent of all flows
        if not new_state_name:
            for flow in self.flows.values():
                if flow.matches_intent(intent):
                    new_state_name = flow.name + '.root'
                    break

        if not new_state_name:
            logging.error('Error! Found intent "%s" but no flow present for it!' % intent)
            return False

        logging.info('Moving based on intent %s...' % intent)
        return self._move_to(new_state_name + ":")  # : runs the action

    def _check_entity_transition(self, entities: dict):
        """ Checks if entity was parsed from current message (and moves if associated state exists)"""
        # FIXME somehow it also uses older entities

        # first check if supported, if yes, abort
        entity_values = self._get_entity_value_tuples(entities)
        if self.get_state().is_supported(entity_values):
            logging.debug("Entity supported, no entity transition")
            return False

        # TODO check states of current flow for 'accepted' first

        new_state_name = None

        # then check if there is a flow that would accept the entity
        for flow in self.flows.values():
            if flow.accepts_message(entities.keys()):
                new_state_name = flow.name + '.root'  # TODO might use a state that accepts it instead?
                break

        if new_state_name:
            logging.info("Moving by entity")
            return self._move_to(new_state_name + ":")

        return False

    def get_flow(self, flow_name=None) -> Flow:
        """Returns a Flow object by its name. Defaults to current flow."""
        if not flow_name:
            flow_name, _ = self.current_state_name.split('.', 1)
        return self.flows.get(flow_name)

    def get_state(self, flow_state_name=None) -> Optional[State]:
        """Returns a State object by its name. Defaults to current state."""
        flow_name, state_name = (flow_state_name or self.current_state_name).split('.', 1)
        flow = self.get_flow(flow_name)
        return flow.get_state(state_name) if flow else None

    def _move_to(self, new_state_name, initializing=False, save_identical=False):
        """Moves to a state by its full name."""
        logging.debug("Trying to move to {}".format(new_state_name))

        # if flow prefix is not present, add the current one
        if isinstance(new_state_name, int):
            new_state = self.context.get_history_state(new_state_name - 1)
            new_state_name = new_state['name'] if new_state else None
        if not new_state_name:
            new_state_name = self.current_state_name

        if new_state_name.count(':'):
            new_state_name, action = new_state_name.split(':', 1)
            action = True
        else:
            action = False

        if '.' not in new_state_name:
            new_state_name = self.current_state_name.split('.')[0] + '.' + new_state_name
        if not self.get_state(new_state_name):
            logging.warning('Error: State %s does not exist! Staying at %s.' % (new_state_name, self.current_state_name))
            return False
        identical = new_state_name == self.current_state_name
        if not initializing and (not identical or save_identical):
            self.context.add_state(new_state_name)
        if not new_state_name:
            return False
        previous_state = self.current_state_name
        self.current_state_name = new_state_name
        if not initializing:

            try:
                if previous_state != new_state_name and action:
                    logging.info("Moving from {} to {} and executing action".format(
                        previous_state, new_state_name
                    ))
                    self._run_accept()
                elif action:
                    logging.info("Staying in state {} and executing action".format(previous_state))
                    self._run_accept()
                elif previous_state != new_state_name:
                    logging.info("Moving from {} to {} and doing nothing".format(previous_state, new_state_name))
                else:
                    logging.info("Staying in state {} and doing nothing".format(previous_state))

            except Exception as e:
                import traceback
                self.context.debug(level=logging.INFO)
                logging.exception(
                              '*****************************************************\n'
                              'Exception occurred in state {}\n'
                              'Message: {}\n'
                              'User: {}\n'
                              'Conversation: {}\n'
                              '*****************************************************'
                              .format(
                                  new_state_name,
                                  self.message.__dict__,
                                  self.message.user.__dict__,
                                  self.message.user.conversation.__dict__
                              )
                )

                # Raise the error if we are in a test
                if self.message.user.conversation.is_test:
                    raise e

                traceback_str = traceback.format_exc()
                if self.send_exceptions:
                    self.dialog.send([
                        TextMessage('Debug: '+str(e)),
                        TextMessage('Debug: '+traceback_str)
                    ])

        return True


    def _get_entity_value_tuples(self, entities: dict, include=tuple()):
        """
        Builds a set of [entity names + tuples (entity_name, entity_value)].
        Used for checking whether the current state supports the received message.
        """
        entity_set = set()
        # add entity names
        entity_set = entity_set.union(entities.keys())

        # add tuples (name, value)
        for name, values in entities.items():
            try:
                if include and name not in include:
                    continue
                for value in values:
                    if 'value' in value:
                        entity_set.add((name, value['value']))
            except Exception:
                # someone set this entity to something special
                logging.debug("Skipping entity {} in supported message check".format(name))

        return entity_set

